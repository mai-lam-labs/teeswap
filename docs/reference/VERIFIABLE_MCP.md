# Verifiable MCP and stateless MCP

A digest of the external specs TEESwap implements. How TEESwap uses them is in
`../DESIGN.md`.

## Extensions (SEP-2133)

SEP-2133 is MCP's extension mechanism: how optional capabilities beyond the
core protocol are named, declared in the `extensions` map, and negotiated.
Verifiable MCP is one such extension.

## Verifiable MCP

**Proposal:** [modelcontextprotocol/modelcontextprotocol#3354](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3354)
**Author:** AkiraTamai (Ripple Node Lab)
**Status:** Pre-SEP feedback, working reference implementation
([ripple-node-lab/mcp-verifiable-tools-demo](https://github.com/ripple-node-lab/mcp-verifiable-tools-demo))

An optional MCP extension (`io.modelcontextprotocol/verifiable-tools`) that lets
servers attach cryptographic evidence to `tools/call` results. Clients verify
locally, without trusting the server operator.

Blind execution has since moved to a companion proposal, `verifiable-tools-blind`
(namespace `io.github.ripple-node-lab/verifiable-tools-blind`). TEESwap's blind
path still uses the base namespace; it's described below as TEESwap implements it.

### Capability negotiation

Advertised in the standard `extensions` map:

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

### Result metadata

Tool results carry proof evidence in `_meta`: the proof format, the proof blob,
public inputs (commitments, nonce), and a verification key reference.

### Commitments

```
inputCommitment  = sha256(salt || JCS(arguments))
outputCommitment = sha256(JCS(content))
```

Public inputs always include both commitments and a nonce, binding inputs,
outputs and proof whatever the proof format.

### Proof formats

Pluggable, identified by string:

| Format | Type | Notes |
|---|---|---|
| `snarkjs-v2` | ZK (Groth16) | Circom circuits, trusted setup required |
| `noir-v1` | ZK (UltraHonk) | No trusted setup |
| `risc0-v1` | zkVM | Rust guest programs, general computation |
| `ezkl-v1` | ZKML (Halo2-KZG) | ML model inference proofs |
| `tee-vaportpm-v1` | TEE attestation | vTPM attestation documents (COSE/CBOR) |
| `tee-sgx-dcap-v1` | TEE attestation | Intel SGX DCAP quotes |
| `tee-sevsnp-v1` | TEE attestation | AMD SEV-SNP attestation reports |
| `oracle-sig-v1` | Input provenance | Ed25519 signatures over upstream data |
| `zktls-tlsn-v1` | Input provenance | TLSNotary presentations of HTTPS responses |

TEE formats have four verifier checks: certificate chain, measurements, key
binding, and freshness.

### Blind execution (HPKE)

The client encrypts tool arguments with HPKE ([RFC 9180](https://www.rfc-editor.org/rfc/rfc9180)),
so the MCP hosting layer never sees plaintext; only the enclave decrypts.

**Scheme:** `hpke-v1`: X25519 / HKDF-SHA256 / AES-128-GCM

1. The server advertises `blindExecution: true` and `blindPublicKeys` in its capabilities.
2. The client encrypts the arguments, `base64url(enc || ciphertext)`, with AAD and info strings.
3. The request carries `encryptedArguments` and a salted `inputCommitment`.
4. The operator's MCP process receives the request but can't read the arguments.
5. Only the proving environment decrypts and executes.
6. The result's proof binds the commitment to the output. With a `replyPublicKey`,
   the reply is encrypted to the client too.

### Input provenance

Proofs can bind upstream data attestations into `publicInputs`: `oracle-sig-v1`
(a signature from a trusted oracle) or `zktls-tlsn-v1` (a TLSNotary presentation
proving data came from a given HTTPS endpoint).

### Deferred proofs

For long operations, a tool returns a `resultId` at once and the client fetches
the proof later via `verifiable-tools/prove`, alongside the
`io.modelcontextprotocol/tasks` extension.

### Verification key pinning

Clients fetch verification keys from an origin-allowlisted registry, never from
the server itself: the server is not the trust root for its own proofs. Keys are
pinned by circuit hash.

## MCP 2026-07-28 (stateless)

[Blog post](https://blog.modelcontextprotocol.io/posts/2026-07-28/) ·
[Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)

| Change | What it means |
|---|---|
| No sessions: `initialize` and `Mcp-Session-Id` removed | Servers can run behind round-robin load balancers |
| Per-request identity: protocol version and capabilities in each request's `_meta` | Every request stands alone |
| `Mcp-Method` / `Mcp-Name` headers | Gateways can route by method and tool |
| Cacheable lists: `ttlMs`, `cacheScope` | Static lists can be cached |
| Multi round-trip requests: `resultType: "input_required"` | A tool can ask for input mid-call |
| Extensions framework | Optional capabilities such as Verifiable MCP, formally |
| Application-level state: servers mint explicit handles | State travels as handles the client holds |
| OAuth hardening: RFC 9207 issuer validation, CIMD replacing DCR | For authenticated access |
