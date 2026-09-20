# TEESwap

Cross-chain swap aggregator running inside a verified TEE.
Every tool result carries a cryptographic attestation (SEP-2133 Verifiable MCP)
proving the TEE executed the correct code on the claimed inputs.

**License:** AGPL-3.0

## Quickstart

```bash
make install    # project-local uv + Python 3.14 + all deps (nothing touches $HOME)
make check      # ruff lint + ruff format + ty + pyrefly strict + pytest
teeswap serve   # HTTP server on 127.0.0.1:8402 (MCP + REST + OpenAPI)
teeswap stdio   # MCP over stdin/stdout (JSONL)
teeswap tools   # list registered tools
teeswap version # package + Python version
```

## Architecture

Tools are defined once (dataclass types + a `Tool` subclass) and auto-exposed via
MCP (stateful 2025-11-25 + stateless 2026-07-28), REST (`POST /teeswap/*`), and
OpenAPI. Every tool result is cryptographically attested (SEP-2133) with Ed25519
proofs and optional HPKE blind execution (RFC 9180). Payments use an invoice
lifecycle (DRAFT → SETTLING → PAID → FULFILLED) with x402 authorization.

## Tools

Register a `Tool` subclass → it appears in MCP `tools/list`, gets a REST endpoint
at `/teeswap/<name>`, and shows up in OpenAPI docs. Types are defined once as
dataclasses with `Annotated[T, Parameter(description=...)]`; schemas derive
automatically via Litestar.

| Tool | Description | Session? | MCP? |
|---|---|---|---|
| `teeswap_routes` | List supported swap routes and tokens | No | Yes |
| `teeswap_quote` | Get a swap quote with risk profile | No | Yes |
| `teeswap_execute` | Execute a quoted swap (x402 payment) | No | Yes |
| `teeswap_status` | Check swap order status | No | Yes |
| `teeswap_refund` | Initiate refund for failed swap | No | Yes |
| `teeswap_invoices` | List invoices for current session | Yes | Yes |
| `teeswap_invoice` | Get invoice details | No | Yes |
| `teeswap_pay` | Submit x402 payment for an invoice | No | Yes |
| `teeswap_receipt_pdf` | Download PDF receipt | No | REST only |

## Verifiable MCP (SEP-2133)

Every `tools/call` response carries an `_meta` block with:
- `inputCommitment` — SHA-256 of canonicalized arguments
- `outputCommitment` — SHA-256 of canonicalized content
- `proof` — Ed25519 signature binding commitments + nonce
- `proofFormat` — `tee-nitro-v1`
- `teeAttestation` — boot-time TPM attestation (when running in TEE)

Blind execution via `verifiable-tools/call`: client encrypts arguments with
HPKE to the server's attested public key; server decrypts, executes, encrypts
the reply. HPKE conformance verified against RFC 9180 A.1.1 test vectors.

## Trust model

Built on [lockboot](https://github.com/lockboot) (stage0→stage1→stage2 verified
boot chain) and [vaportpm](https://github.com/lockboot/vaportpm) (vTPM
attestation). See `docs/TRUST_COMPARISON.md` for a steelman comparison
against Phala/dstack, RA-TLS, and the SEP-2133 reference implementation.

## Dev toolchain

- Python 3.14 targeting PEP 695 generics, PEP 649 deferred annotations
- ruff (lint + format), ty + pyrefly strict (type checking), pytest
- `pyproject.toml` as single source of truth (package metadata, deps, version)
- `make check` = 5 gates, all must pass
