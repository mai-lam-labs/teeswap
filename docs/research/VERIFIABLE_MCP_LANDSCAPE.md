# Verifiable MCP & TEE attestation: the landscape

**Date:** 2026-09-17
**Purpose:** Survey of verifiable execution, TEE attestation, and trust establishment in the MCP ecosystem: what exists, and where TEESwap fits. The specs themselves are digested in `../reference/VERIFIABLE_MCP.md`.

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

## 2. Reference Implementation

**Repo:** [ripple-node-lab/mcp-verifiable-tools-demo](https://github.com/ripple-node-lab/mcp-verifiable-tools-demo)

### 2.1 Architecture

- TypeScript SDK adapter on MCP `2026-07-28`
- Streamable HTTP transport
- Non-TypeScript provers run as HTTP sidecars under Docker Compose:
  - Rust sidecar for RISC Zero zkVM (`risc0-v1`)
  - Python sidecar for ezkl ZKML (`ezkl-v1`)
  - Rust sidecar for TLSNotary (`zktls-tlsn-v1`)

### 2.2 Implementation status

| Phase | Status | What |
|---|---|---|
| Phase 1 | Done | Core extension, demo formats, capability negotiation |
| Phase 2-a | Done | Result binding, nonce, HPKE blind execution, deferred proofs |
| Phase 2-b | Done | Real ZK formats (snarkjs-v2 Groth16, noir-v1 UltraHonk) |
| Phase 3-d | Done | Input provenance (oracle-sig-v1, zktls-tlsn-v1 TLSNotary) |
| Phase 4-a | Done | CI-tested, 10 end-to-end demo scenarios |
| Phase 4-b | Pending | Rebase SDK adapter on v2 server/discover surface (awaiting SDK release) |

### 2.3 Performance

- RISC Zero proving: ~20s per proof on CPU
- ezkl: proving key regeneration at startup ~1 minute
- Demo is teaching-quality, not production-grade

---

## 3. Existing TEE + MCP Projects

### 3.1 Phala Cloud / dstack

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

### 3.2 DeMCP — Decentralized MCP Network

**Link:** [Phala x DeMCP](https://phala.com/posts/phala-x-demcp-leveraging-tee-for-decentralized-mcp-network)

**What it is:** decentralized MCP network with crypto payments (USDC/USDT), TEE-backed security, and blockchain service registries.

**Architecture:** Service providers build MCP services → deploy in TEE (Phala Cloud) → registered on blockchain → developers access via centralized API → payment in stablecoins.

**Relevance:** DeMCP + x402 is conceptually close to our distribution model, but they're focused on general MCP services while we're focused on financial execution.

### 3.3 Attestable MCP Server (kontext-dev)

**Repo:** [co-browser/attestable-mcp-server](https://github.com/kontext-dev/attestable-mcp-server)

**What it is:** MCP server implementing RA-TLS (Remote Attestation TLS) — an extension to TLS that embeds machine and code measurements.

**How it works:**
- Certificate contains an SGX quote in an X.509 extension using TCG DICE "tagged evidence" OID
- Quote includes SGX report + Intel certificate chain
- Certificate embeds `pubkey-hash` — hash of the ephemeral public key generated by the TEE
- MCP clients can remotely attest the code running on any MCP server

**Platform:** Intel SGX, Ubuntu 22.04, Gramine runtime.

**Relevance:** RA-TLS is an alternative to Verifiable MCP for establishing trust. It operates at the transport layer (TLS handshake) rather than the application layer (tool result metadata). Could be complementary — RA-TLS for session trust, Verifiable MCP for per-result proofs.

### 3.4 MEOK Attestation Verify

**Link:** [mcpservers.org listing](https://mcpservers.org/servers/csoai-org/meok-attestation-verify)

**What it is:** verifier for HMAC-signed compliance attestations covering DORA, NIS2, CRA, EU AI Act, CSRD, AI-BOM standards.

**Relevance:** Compliance-focused, not TEE-focused. Uses HMAC-SHA256 rather than hardware attestation. Different trust model (software signing vs hardware measurement).

---

## 4. Landscape Summary

```
                          TRUST ESTABLISHMENT
                          
  Transport-layer              Application-layer             Hosting-layer
  ┌─────────────┐             ┌──────────────────┐          ┌────────────┐
  │  RA-TLS     │             │ Verifiable MCP   │          │ Phala Cloud│
  │  (SGX DCAP  │             │ (extension)      │          │ (dstack)   │
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
| TEE attestation spec for MCP | Yes (Verifiable MCP, `tee-vaportpm-v1`) | Implement the spec using vaportpm |
| HPKE blind execution spec | Yes (Verifiable MCP, `hpke-v1`) | Implement — gives us E2EE for free |
| Reference implementation | Yes (ripple-node-lab demo) | Study, don't fork — it's teaching code |
| TEE MCP hosting | Yes (Phala/dstack) | We have our own stack |
| RA-TLS for MCP | Yes (attestable-mcp-server) | Complementary option, SGX-only |
| Local attestation proxy | **No** | Build using vaportpm-verify |
| All-inclusive swap execution | **No** | TEESwap — the application layer |
| Input provenance for price feeds | Spec exists (oracle-sig-v1) | Implement to prove quotes are real |

---

## 5. Sources

### Specifications & Proposals
- [Verifiable MCP proposal (#3354)](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3354)
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
