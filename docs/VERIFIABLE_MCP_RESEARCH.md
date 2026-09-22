# Verifiable MCP & TEE Attestation Research

**Date:** 2026-09-17
**Purpose:** Survey of verifiable execution, TEE attestation, and trust establishment in the MCP ecosystem — what exists, what specs are emerging, and how TEESwap + vaportpm fit into the landscape.

---

## 1. The Problem

MCP has no built-in way to verify that a tool result is correct. A client receives a response but nothing tells it whether the value came from the advertised function, a stale cache, or was fabricated. For price feeds, compliance checks, and financial operations this is unacceptable.

The [Phala "MCP Not Safe" analysis](https://phala.com/posts/MCP-Not-Safe-Reasons-and-Ideas) catalogues the attack surface:
- **Tool poisoning:** malicious instructions hidden in tool descriptions that the LLM executes
- **Rug pull redefinition:** tools mutate after installation without detection (no integrity checks or signing)
- **Cross-server shadowing:** rogue servers override legitimate tools from other servers
- **Command injection:** 43% of MCP servers tested had unsafe shell calls
- **Token theft:** compromised servers steal stored auth tokens silently

Root cause: MCP was designed for flexibility, not security. No standard authentication between client and server, no context encryption, no integrity verification for tool definitions.

---

## 2. Verifiable MCP — SEP-2133

**Proposal:** [modelcontextprotocol/modelcontextprotocol#3354](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3354)
**Author:** AkiraTamai (Ripple Node Lab)
**Status:** Pre-SEP feedback, working reference implementation

### 2.1 What it defines

An optional MCP extension (`io.modelcontextprotocol/verifiable-tools`) that allows servers to attach cryptographic evidence to `tools/call` results. Clients verify locally — no blind trust in the server operator.

### 2.2 Capability negotiation

Advertised via the standard `extensions` map during connection:

```json
{
  "extensions": {
    "io.modelcontextprotocol/verifiable-tools": {
      "proofFormats": ["tee-vaportpm-v1", "snarkjs-v2"],
      "blindExecution": true,
      "resultTtlMs": 30000
    }
  }
}
```

### 2.3 Result metadata

Tool results carry structured proof evidence in `_meta`:
- Proof format identifier
- Cryptographic proof blob
- Public inputs (commitments, nonce)
- Verification key reference

### 2.4 Commitment scheme

```
inputCommitment  = sha256(salt || JCS(arguments))
outputCommitment = sha256(JCS(content))
```

Public inputs always include both commitments plus a nonce, providing format-independent binding between inputs, outputs, and proof.

### 2.5 Proof formats

Pluggable, identified by string:

| Format | Type | Notes |
|---|---|---|
| `snarkjs-v2` | ZK (Groth16) | Circom circuits, trusted setup required |
| `noir-v1` | ZK (UltraHonk) | No trusted setup |
| `risc0-v1` | zkVM | Rust guest programs, general computation |
| `ezkl-v1` | ZKML (Halo2-KZG) | ML model inference proofs |
| `tee-vaportpm-v1` | TEE attestation | AWS Nitro COSE/CBOR attestation documents |
| `tee-sgx-dcap-v1` | TEE attestation | Intel SGX DCAP quotes |
| `tee-sevsnp-v1` | TEE attestation | AMD SEV-SNP attestation reports |
| `oracle-sig-v1` | Input provenance | Ed25519 signatures over upstream data |
| `zktls-tlsn-v1` | Input provenance | TLSNotary presentations of HTTPS responses |

**TEE formats include four verifier checks:** certificate chain validation, measurement verification, key binding, and freshness.

### 2.6 Blind execution (HPKE)

The most relevant feature for TEESwap. The client encrypts tool arguments using HPKE ([RFC 9180](https://www.rfc-editor.org/rfc/rfc9180)) so the MCP hosting layer never sees plaintext — only the TEE enclave decrypts.

**Scheme:** `hpke-v1` — X25519 / HKDF-SHA256 / AES-128-GCM

**Flow:**
1. Server advertises `blindExecution: true` and `blindPublicKeys` in capabilities
2. Client encrypts arguments: `base64url(enc || ciphertext)` with AAD and info strings
3. Client includes `encryptedArguments` + salted `inputCommitment` in the request
4. Operator's MCP process receives the request but **cannot read the arguments**
5. Only the proving environment (TEE enclave) decrypts and executes
6. Result includes proof binding the commitment to the output

**Why this matters:** E2EE between client and TEE without a custom protocol. The hosting infrastructure (load balancer, API gateway, MCP process) never sees swap parameters, amounts, or recipient addresses.

### 2.7 Input provenance

Proofs can bind upstream data attestations into `publicInputs`:
- **oracle-sig-v1:** Ed25519 signature from a trusted price oracle, bound into the proof
- **zktls-tlsn-v1:** TLSNotary presentation proving data came from a specific HTTPS endpoint

For TEESwap, this could prove that a price quote actually came from the CoW API or Across API, not fabricated by the TEE.

### 2.8 Deferred proofs

For long-running operations (like cross-chain swaps), proofs can be generated asynchronously:
- Tool returns immediately with a `resultId`
- Client retrieves proof later via `verifiable-tools/prove`
- Integrates with the `io.modelcontextprotocol/tasks` extension for async tracking

### 2.9 Verification key pinning

Clients fetch verification keys from an **origin-allowlisted registry**, not from the server itself. The server is never the trust root for its own proof. Keys are pinned by circuit hash — mismatched tool-to-circuit mappings are rejected.

---

## 3. Reference Implementation

**Repo:** [ripple-node-lab/mcp-verifiable-tools-demo](https://github.com/ripple-node-lab/mcp-verifiable-tools-demo)

### 3.1 Architecture

- TypeScript SDK adapter on MCP `2026-07-28`
- Streamable HTTP transport
- Non-TypeScript provers run as HTTP sidecars under Docker Compose:
  - Rust sidecar for RISC Zero zkVM (`risc0-v1`)
  - Python sidecar for ezkl ZKML (`ezkl-v1`)
  - Rust sidecar for TLSNotary (`zktls-tlsn-v1`)

### 3.2 Implementation status

| Phase | Status | What |
|---|---|---|
| Phase 1 | Done | Core extension, demo formats, capability negotiation |
| Phase 2-a | Done | Result binding, nonce, HPKE blind execution, deferred proofs |
| Phase 2-b | Done | Real ZK formats (snarkjs-v2 Groth16, noir-v1 UltraHonk) |
| Phase 3-d | Done | Input provenance (oracle-sig-v1, zktls-tlsn-v1 TLSNotary) |
| Phase 4-a | Done | CI-tested, 10 end-to-end demo scenarios |
| Phase 4-b | Pending | Rebase SDK adapter on v2 server/discover surface (awaiting SDK release) |

### 3.3 Performance

- RISC Zero proving: ~20s per proof on CPU
- ezkl: proving key regeneration at startup ~1 minute
- Demo is teaching-quality, not production-grade

---

## 4. MCP 2026-07-28 Specification (Stateless)

**Blog post:** [modelcontextprotocol.io/posts/2026-07-28](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
**Changelog:** [modelcontextprotocol.io/specification/2026-07-28/changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)

The July 2026 spec revision makes MCP stateless. Key changes:

| Change | Impact for TEESwap |
|---|---|
| **No sessions** — `initialize` handshake and `Mcp-Session-Id` removed | TEESwap can run behind round-robin load balancers, no sticky sessions |
| **Per-request identity** — each request carries protocol version + capabilities in `_meta` | Attestation can be verified per-request if needed |
| **Header-based routing** — `Mcp-Method` and `Mcp-Name` headers | Gateways can route quote/execute/status to different backend pools |
| **Cacheable lists** — `ttlMs` + `cacheScope` on list responses | `teeswap_routes` (static data) can be cached aggressively |
| **MRTR** — Multi Round-Trip Requests via `resultType: "input_required"` | Swap confirmation flow can use MRTR instead of separate execute tool |
| **Extensions framework** — formal mechanism for optional capabilities | Verifiable MCP is an extension, not a core protocol change |
| **Application-level state** — servers mint explicit handles (like `orderId`) | Our `quoteId`/`orderId` pattern is the canonical approach |
| **OAuth hardening** — RFC 9207 issuer validation, CIMD replacing DCR | Relevant if we add authenticated access tiers |

**Stateless + Verifiable MCP together:** each request is independently verifiable (carries its own attestation proof) AND stateless (no session to hijack). This is the ideal architecture for TEESwap — every tool result is cryptographically bound to its inputs, and the server can scale horizontally without coordination.

---

## 5. Existing TEE + MCP Projects

### 5.1 Phala Cloud / dstack

**Links:**
- [dstack — Open-Source TEE Runtime](https://phala.com/dstack)
- [Deploy MCP Server on Phala Cloud](https://phala.network/deploy-an-MCP-server-on-phala-cloud-a-step-by-step-guide)
- [Securely Deploy Crypto Wallet MCP Server](https://phala.com/posts/developer-guide-securely-deploy-a-crypto-wallet-mcp-server-on-phala-cloud)
- [Phala x DeMCP](https://phala.com/posts/phala-x-demcp-leveraging-tee-for-decentralized-mcp-network)

**What it is:** TEE-backed MCP hosting platform. Docker containers run inside hardware enclaves (GCP Confidential VMs, AWS Nitro) with attestation reports baked in.

**Architecture:**
- dstack SDK handles TEE provisioning, attestation generation, encrypted credential storage
- One-click deploy from open-source MCP server templates
- Remote attestation: every deployed app gets an attestation report proving genuine TEE + untampered code
- Works across AWS, Google Cloud, and Phala's own infrastructure

**Relevance:** Phala is a hosting platform, not an application. TEESwap would be a potential app ON Phala Cloud, but we have our own TEE stack (vaportpm). The dstack attestation pipeline had [security issues disclosed Feb 2026](https://phala.com/posts/dstack-security-update-attestation-pipeline-hardening) — worth studying for lessons learned.

### 5.2 DeMCP — Decentralized MCP Network

**Link:** [Phala x DeMCP](https://phala.com/posts/phala-x-demcp-leveraging-tee-for-decentralized-mcp-network)

**What it is:** decentralized MCP network with crypto payments (USDC/USDT), TEE-backed security, and blockchain service registries.

**Architecture:** Service providers build MCP services → deploy in TEE (Phala Cloud) → registered on blockchain → developers access via centralized API → payment in stablecoins.

**Relevance:** DeMCP + x402 is conceptually close to our distribution model, but they're focused on general MCP services while we're focused on financial execution.

### 5.3 Attestable MCP Server (kontext-dev)

**Repo:** [co-browser/attestable-mcp-server](https://github.com/kontext-dev/attestable-mcp-server)

**What it is:** MCP server implementing RA-TLS (Remote Attestation TLS) — an extension to TLS that embeds machine and code measurements.

**How it works:**
- Certificate contains an SGX quote in an X.509 extension using TCG DICE "tagged evidence" OID
- Quote includes SGX report + Intel certificate chain
- Certificate embeds `pubkey-hash` — hash of the ephemeral public key generated by the TEE
- MCP clients can remotely attest the code running on any MCP server

**Platform:** Intel SGX, Ubuntu 22.04, Gramine runtime.

**Relevance:** RA-TLS is an alternative to Verifiable MCP for establishing trust. It operates at the transport layer (TLS handshake) rather than the application layer (tool result metadata). Could be complementary — RA-TLS for session trust, Verifiable MCP for per-result proofs.

### 5.4 MEOK Attestation Verify

**Link:** [mcpservers.org listing](https://mcpservers.org/servers/csoai-org/meok-attestation-verify)

**What it is:** verifier for HMAC-signed compliance attestations covering DORA, NIS2, CRA, EU AI Act, CSRD, AI-BOM standards.

**Relevance:** Compliance-focused, not TEE-focused. Uses HMAC-SHA256 rather than hardware attestation. Different trust model (software signing vs hardware measurement).

---

## 6. Landscape Summary

```
                          TRUST ESTABLISHMENT
                          
  Transport-layer              Application-layer             Hosting-layer
  ┌─────────────┐             ┌──────────────────┐          ┌────────────┐
  │  RA-TLS     │             │ Verifiable MCP   │          │ Phala Cloud│
  │  (SGX DCAP  │             │ (SEP-2133)       │          │ (dstack)   │
  │   in X.509) │             │                  │          │            │
  │             │             │ Per-result proofs │          │ TEE hosting│
  │  attestable-│             │ ZK + TEE + HPKE  │          │ attestation│
  │  mcp-server │             │                  │          │ baked in   │
  └──────┬──────┘             │ ripple-node-lab  │          └─────┬──────┘
         │                    │ reference impl   │                │
         │                    └────────┬─────────┘                │
         │                             │                          │
         └─────────────┬───────────────┘                          │
                       │                                          │
              ┌────────▼────────────────────────────────┐         │
              │         TEESwap                         │         │
              │                                         │         │
              │  vaportpm-attest (attestation gen)       │◄────────┘
              │  + Verifiable MCP tee-vaportpm-v1 proofs   │  (we run our
              │  + HPKE blind execution                 │   own infra)
              │  + vaportpm-verify in local proxy       │
              └─────────────────────────────────────────┘
```

### What exists vs. what we build

| Component | Exists? | Our approach |
|---|---|---|
| TEE attestation spec for MCP | Yes (SEP-2133, `tee-vaportpm-v1`) | Implement the spec using vaportpm |
| HPKE blind execution spec | Yes (SEP-2133, `hpke-v1`) | Implement — gives us E2EE for free |
| Reference implementation | Yes (ripple-node-lab demo) | Study, don't fork — it's teaching code |
| TEE MCP hosting | Yes (Phala/dstack) | We have our own stack |
| RA-TLS for MCP | Yes (attestable-mcp-server) | Complementary option, SGX-only |
| Local attestation proxy | **No** | Build using vaportpm-verify |
| All-inclusive swap execution | **No** | TEESwap — the application layer |
| Input provenance for price feeds | Spec exists (oracle-sig-v1) | Implement to prove quotes are real |

---

## 7. Implications for TEESwap

### 7.1 Implement Verifiable MCP, don't invent a protocol

SEP-2133 already defines:
- How to attach TEE attestations to MCP tool results (`tee-vaportpm-v1`)
- How to encrypt arguments so hosting can't read them (`hpke-v1` blind execution)
- How to prove input data came from real APIs (`oracle-sig-v1`, `zktls-tlsn-v1`)
- How to handle async proof generation for long operations (deferred proofs + tasks)

This is exactly what we need. The implementation is `vaportpm-attest` generating the attestation document, formatted as `tee-vaportpm-v1` proof metadata on tool results.

### 7.2 The local proxy is a Verifiable MCP client

The distributable proxy binary:
1. Embeds `vaportpm-verify` (pure Rust, zero C deps)
2. Implements Verifiable MCP client-side: verifies `tee-vaportpm-v1` proofs on every tool result
3. Handles HPKE encryption of arguments (blind execution)
4. Exposes plain MCP locally (stdio/HTTP) — the upstream agent doesn't need to understand verification
5. Adds Verifiable MCP support to any MCP client that doesn't natively support SEP-2133

This is a **standard implementation**, not a proprietary protocol.

### 7.3 Stateless MCP (2026-07-28) fits perfectly

- TEESwap is already designed around explicit handles (`quoteId`, `orderId`)
- No session state to manage — each request is self-contained
- Horizontal scaling behind load balancers with no coordination
- Header-based routing: quote requests to one pool, execution to another
- Cacheable `teeswap_routes` responses via `ttlMs`

### 7.4 Per-result attestation proves execution integrity

Every tool result includes proof that:
- The TEE is running the attested code (enclave measurement)
- The result was computed from the actual inputs (commitment binding)
- Price data came from real APIs (input provenance via oracle-sig or zktls-tlsn)
- The hosting infrastructure didn't tamper with arguments or results (HPKE blind execution)

This is stronger than any existing swap aggregator can offer. No competitor has per-operation cryptographic proof of correct execution.

### 7.5 Deferred proofs for cross-chain operations

A cross-chain swap takes minutes. The flow:
1. `teeswap_execute` returns immediately with `orderId` + initial attestation (deposit submitted)
2. Client polls `teeswap_status` — each status update includes fresh attestation
3. On completion, full proof is available via deferred proof retrieval
4. Proof chain covers: deposit → bridge fill → delivery (each step attested)

---

## 8. Sources

### Specifications & Proposals
- [Verifiable MCP (SEP-2133) — GitHub Issue](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3354)
- [MCP 2026-07-28 Specification Blog Post](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [MCP 2026-07-28 Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [MCP 2026-07-28 Release Candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)

### Reference Implementations
- [ripple-node-lab/mcp-verifiable-tools-demo](https://github.com/ripple-node-lab/mcp-verifiable-tools-demo)
- [co-browser/attestable-mcp-server (RA-TLS)](https://github.com/kontext-dev/attestable-mcp-server)

### TEE + MCP Ecosystem
- [Phala "MCP Not Safe — Reasons and Ideas"](https://phala.com/posts/MCP-Not-Safe-Reasons-and-Ideas)
- [Phala dstack — Open-Source TEE Runtime](https://phala.com/dstack)
- [Deploy MCP Server on Phala Cloud](https://phala.network/deploy-an-MCP-server-on-phala-cloud-a-step-by-step-guide)
- [Crypto Wallet MCP Server on Phala Cloud](https://phala.com/posts/developer-guide-securely-deploy-a-crypto-wallet-mcp-server-on-phala-cloud)
- [Phala x DeMCP — TEE for Decentralized MCP](https://phala.com/posts/phala-x-demcp-leveraging-tee-for-decentralized-mcp-network)
- [dstack Security Update — Attestation Pipeline Hardening](https://phala.com/posts/dstack-security-update-attestation-pipeline-hardening)

### x402 + MCP Ecosystem
- [x402-mcp — 100+ Bazaar APIs as MCP tools](https://github.com/x402node/x402-mcp)
- [x402-bazaar-mcp — 131 pay-per-call APIs](https://github.com/sukrutkrdg/x402-bazaar-mcp)
- [Cloudflare agents x402 integration](https://github.com/cloudflare/agents/pull/2273)
- [Zuplo — Autonomous MCP Payments with x402](https://zuplo.com/blog/mcp-api-payments-with-x402)
- [systemprompt.io — Monetize MCP Server with x402](https://systemprompt.io/guides/monetize-mcp-server-x402)

### MCP Authentication & Security
- [Stack Overflow — MCP Authentication and Authorization](https://stackoverflow.blog/2026/01/21/is-that-allowed-authentication-and-authorization-in-model-context-protocol/)
- [Equixly — Stateless MCP Security](https://equixly.com/blog/2026/08/05/stateless-mcp/)
- [securew2 — MCP Server Security Complete Guide](https://securew2.com/blog/mcp-server-security)
- [MCP OAuth 2.1 / PKCE](https://aembit.io/blog/mcp-oauth-2-1-pkce-and-the-future-of-ai-authorization/)
- [Agent Identity Protocol (AIP) paper](https://arxiv.org/pdf/2603.24775)

### Stateless MCP Analysis
- [Google Developers — Scaling AI Agent Infrastructure with MCP Stateless](https://developers.googleblog.com/scaling-ai-agent-infrastructure-with-the-mcp-stateless-updates/)
- [Appwrite — MCP Goes Stateless](https://appwrite.io/blog/post/mcp-goes-stateless-in-the-2026-07-28-specification)
- [x402 + A2A + MCP Three-Protocol Stack](https://bex.co/blog/2026/03/22/x402-a2a-mcp-three-protocol-stack-autonomous-agent-commerce-infrastructure)

### Compliance Attestation (Adjacent)
- [MEOK Attestation Verify MCP Server](https://mcpservers.org/servers/csoai-org/meok-attestation-verify)
- [MEOK Compliance MCP Catalogue](https://meok-attestation-api.vercel.app/)
