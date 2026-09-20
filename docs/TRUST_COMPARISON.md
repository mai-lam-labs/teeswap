# TEE Trust Model Comparison: lockboot vs. Alternatives

**Date:** 2026-09-18
**Purpose:** Steelman the lockboot approach against the existing TEE + MCP implementations. Not a marketing document — an honest comparison including lockboot's own limitations.

---

## The implementations compared

| Approach | Used by | Trust root | Key binding | Per-result attestation |
|---|---|---|---|---|
| **lockboot (stage0→1→2) + vaportpm** | TEESwap | Cloud vTPM + full boot chain PCR | PCR extension | Yes (Verifiable MCP) |
| **Phala dstack** | Phala Cloud, DeMCP | Intel SGX/TDX DCAP | Attestation report | No (connection-time only) |
| **RA-TLS (Gramine)** | attestable-mcp-server | Intel SGX MRENCLAVE | X.509 extension | No (TLS handshake only) |
| **SEP-2133 reference impl** | ripple-node-lab demo | Mock (no real hardware) | user_data field | Yes (mock proofs) |

---

## 1. Boot chain coverage

**The question:** How much of the stack between "source code" and "running process" is measured?

**Phala/dstack:** You push a Docker image. dstack runs it inside a Confidential VM. The attestation proves the VM's launch measurement, which includes the dstack runtime + your container image hash. But:
- The Docker image build is your CI, not verified by the platform
- The dstack runtime itself is what's measured at the hardware level — your code is a layer on top
- The boot chain below dstack (firmware, hypervisor setup) is implicit trust in the platform

**RA-TLS (Gramine):** Verifies MRENCLAVE — a hash of the enclave binary. This covers the application code loaded into the enclave, but not the system that loaded it. Gramine itself is part of the TCB and has had vulnerabilities.

**lockboot:** The entire chain is measured into PCR 14:
- **stage0** (UEFI) → measures and verifies stage1
- **stage1** (UKI, PID 1) → measures and verifies stage2
- **stage2** (container loader) → mounts rootfs via dm-verity, measures the container image

Each stage verifies the next before executing it. The rootfs runs on dm-verity — every block is hash-verified on read, not just at load time. There is no unmeasured gap between "what was built" and "what is running."

**Advantage:** lockboot measures the full stack. Alternatives measure only the enclave/container layer and trust the layers below.

---

## 2. Reproducible builds and operator separation

**The question:** Can the operator substitute different code than what was audited?

**Phala/dstack:** The operator (or developer) pushes the Docker image to Phala Cloud. If the operator is the developer, there's no separation. If they're different parties, the developer trusts Phala Cloud's infrastructure to run their image unmodified — which it does (the attestation proves image hash), but the operator controls which image gets pushed.

**RA-TLS:** The operator deploys the enclave. The client checks MRENCLAVE against a known-good value. This works, but the client needs an out-of-band source for the expected MRENCLAVE. If the client just accepts whatever MRENCLAVE the server advertises, there's no protection.

**lockboot:**
- The UKI is built by **GitHub CI** (not the operator's CI)
- Published as a **GitHub release** with expected PCR values
- The cloud VM netboots the UKI directly from the release URL via stage0
- stage0 verifies the payload hash/signature before executing
- The operator provides cloud infrastructure and metadata, but never handles the binary

The operator cannot substitute code because they never touch the binary — it flows directly from GitHub CI to the VM via HTTP, verified at every step. Changing the URL in cloud metadata would require the new binary to match the pinned hash or ed25519 signature.

**Advantage:** lockboot provides structural operator separation. Alternatives rely on the operator to honestly deploy the right code.

---

## 3. Key binding strength

**The question:** How do you know the HPKE public key you're encrypting to actually lives inside the TEE?

**SEP-2133 spec (as written):** Put the key in the attestation document's `user_data` field. `user_data` is **signed** by the attestation (the platform certifies this document was produced by this enclave) but it is **not measured** — it's a runtime parameter supplied at attestation time. The spec acknowledges this: *"the key is only as trustworthy as the `server/discover` channel."*

The risk: a compromised attestation pipeline could inject a different key into `user_data`. This is not theoretical — dstack's QE identity validation bug (Jan 2026) meant quotes from unauthorized sources could be accepted, which would undermine any key binding in `user_data`.

**Phala/dstack:** Same as above — key in report data, signed but not measured. Plus the [actual vulnerabilities found](https://phala.com/posts/dstack-security-update-attestation-pipeline-hardening):
- QE identity verification was not mandatory (Critical)
- TCB status was informational only (High)
- TLS verification defaulted to off in production
- Client-controlled PCCS URL allowed SSRF

**RA-TLS:** The TLS public key is embedded in an X.509 extension alongside the SGX quote. The quote's report_data contains the key hash. This is measured in the sense that the enclave generates the key and puts its hash in report_data, which is part of the signed report. But it's still a runtime value, not a boot-time measurement.

**lockboot:** The HPKE key is extended into a **TPM PCR**. PCRs are hardware registers — once extended, the value cannot be rolled back without a reboot (which re-runs the entire measured boot chain). The attestation document includes the PCR values, signed by the TPM's attestation key, which chains to the cloud provider's root CA.

A compromised process cannot change the PCR value after boot. The only way to get a different key into the PCR is to run different code — which changes the code PCRs, failing the release pinning check.

**Advantage:** PCR extension is a hardware-enforced binding. `user_data` is a software-supplied field. The difference matters precisely when the attestation pipeline has bugs — which [it has](https://phala.com/posts/dstack-security-update-attestation-pipeline-hardening).

---

## 4. Per-result vs. per-connection attestation

**The question:** If the server is compromised AFTER the initial attestation, do subsequent results carry proof?

**Phala/dstack:** Attestation happens at connection time (or on demand). Individual tool results are not attested. A compromised server after the handshake can return fabricated results.

**RA-TLS:** Same — attestation is in the TLS handshake. Once the connection is established, every response is trusted implicitly.

**lockboot + Verifiable MCP:** Every tool result carries a `tee-nitro-v1` attestation in `_meta`. Each result has:
- `inputCommitment` binding it to the specific request
- `outputCommitment` binding it to the specific response
- `nonce` preventing replay
- Fresh attestation document

A compromised server cannot produce valid attestations for fabricated results because it doesn't have the enclave's Ed25519 signing key (which is in application memory, attested via PCR).

**Advantage:** Per-result attestation means every individual operation is independently verifiable, not just the connection.

---

## 5. Rootfs integrity

**The question:** Can the running filesystem be modified at runtime?

**Phala/dstack:** Docker overlayfs. The container image layer is read-only, but the overlay is writable. Runtime modifications are possible within the container's filesystem.

**RA-TLS/Gramine:** Gramine provides a shielded filesystem with integrity checking for specified files, but it's application-configured. Files outside the manifest aren't protected.

**lockboot (stage2):** The rootfs is **dm-verity** (erofs, read-only, every block hash-verified on read) with an ephemeral tmpfs overlay. The verity'd lower layer is immutable — it cannot be modified, period. The overlay is tmpfs (in-memory, lost on reboot). Persistent data lives in `/data` which is **dm-crypt** with TPM-bound keys — a PCR change destroys access (garbage decryption).

**Advantage:** dm-verity provides continuous runtime integrity verification, not just load-time. dm-crypt with PCR-bound keys makes code changes self-destructive.

---

## 6. Execution isolation (TOCTOU)

**The question:** Can the binary be swapped between verification and execution?

**Phala/dstack:** The Docker image is loaded into the VM. Standard Linux execution from the filesystem. Theoretically vulnerable to TOCTOU if the filesystem is writable before the process starts.

**RA-TLS/Gramine:** Gramine's trusted loader handles this for the enclave binary, but shim vulnerabilities have been found in the past.

**lockboot (stage1):** Executes stage2 from a **sealed memfd** (in-memory file descriptor). The binary is never written to disk. There is no window between verification and execution where the binary could be swapped.

**Advantage:** memfd execution eliminates TOCTOU. The binary goes from network → memory → verification → execution with no filesystem step.

---

## 7. PCR verification discipline

**The question:** Does the verifier actually check the right things?

**Phala/dstack (pre-fix):** Verification was user-configured. [QE identity was not mandatory](https://phala.com/posts/dstack-security-update-attestation-pipeline-hardening). TCB status was informational. TLS verification defaulted to off. The fix was "secure by default" — but the point is it shipped with these gaps.

**RA-TLS:** The client must check MRENCLAVE against a known-good value. Most RA-TLS libraries provide the verification function but don't enforce that the client actually pins a measurement. If the client just checks "is this a valid SGX quote?" without checking WHAT was measured, it proves nothing.

**lockboot (vaportpm-verify):** Separates cryptographic validation (automatic, handled by the library) from policy (PCR pins, nonce freshness — explicitly required from the caller). The verify function requires pinned PCR values as input. There is no "verify without checking what was measured" mode — if you don't provide pins, verification fails.

The proxy enforces this: it reads expected code PCRs from the GitHub release and refuses to proceed if they don't match. There is no permissive fallback.

**Advantage:** vaportpm makes the right thing mandatory and the wrong thing impossible. Alternatives make the right thing possible and the wrong thing easy.

---

## 8. Platform independence

**The question:** Does it work on multiple cloud providers?

**Phala/dstack:** Intel SGX/TDX primarily. [GCP, AWS, and Phala's own infra](https://phala.com/posts/dstack-one-confidential-compute-framework-aws-google-cloud-phala). But the attestation format is Intel DCAP, which ties it to Intel silicon.

**RA-TLS:** Intel SGX only. Requires SGX-capable hardware + Gramine runtime.

**lockboot + vaportpm:** Cloud vTPM attestation. Works on any cloud provider with vTPM support:
- AWS EC2 (Nitro v4+) ✅
- GCP Confidential VM ✅
- Azure Trusted Launch 🔜

Not tied to Intel SGX. The root of trust is the cloud provider's hypervisor, not a specific CPU instruction set. This means ARM instances, AMD SEV-SNP, and future platforms are reachable.

**Advantage:** vTPM is a more universal trust anchor than SGX DCAP. lockboot is multi-cloud and multi-architecture.

---

## 9. What lockboot does NOT solve

Honest assessment — these are shared limitations with all TEE approaches:

**Physical access attacks.** The [Flashbots "Mind the Gap" analysis](https://writings.flashbots.net/mind-the-gap-tee-poc) shows that TEE attestation proves WHAT code runs but not WHERE it runs. An attacker with physical access to hardware can extract keys via memory bus interposition. A machine in a garage produces the same attestation as one in a data center. lockboot does not solve this — it's a fundamental limitation of cloud TEE.

**Cloud provider as root of trust.** The vTPM is provided by the hypervisor. A malicious cloud provider could theoretically forge attestations. This is the same trust assumption everyone makes (including SGX, which trusts Intel). lockboot makes the dependency explicit rather than hiding it.

**Relay/proxy attacks.** An attacker could relay attestation requests between a legitimate server and an attacker-controlled one, producing cryptographically valid attestation for the wrong machine. PCR binding helps (the key PCR would differ) but doesn't eliminate the attack class entirely.

**Side channels.** Timing attacks, cache attacks, and speculative execution attacks against TEEs are known. lockboot runs on Confidential VMs (which use memory encryption) but cannot guarantee resistance to all microarchitectural side channels.

**Complexity.** lockboot is more complex to deploy than "push a Docker container to Phala." The staged boot chain requires cloud metadata configuration, UEFI installation, and understanding of the PCR layout. This is a real operational cost.

---

## Summary

| Property | lockboot | Phala/dstack | RA-TLS | SEP-2133 ref |
|---|---|---|---|---|
| Full boot chain measured | ✅ stage0→1→2→rootfs | ❌ Container layer only | ❌ Enclave only | N/A (mock) |
| Operator separated from code | ✅ GitHub CI → netboot | ❌ Operator pushes image | ❌ Operator deploys | N/A |
| Key binding | ✅ PCR (hardware) | ⚠️ user_data (software) | ⚠️ report_data | ⚠️ user_data |
| Per-result attestation | ✅ Every tool result | ❌ Connection only | ❌ TLS handshake only | ✅ (mock) |
| Rootfs integrity (runtime) | ✅ dm-verity continuous | ❌ overlayfs writable | ⚠️ Gramine manifest | N/A |
| TOCTOU elimination | ✅ memfd execution | ❌ Filesystem exec | ⚠️ Trusted loader | N/A |
| Mandatory PCR verification | ✅ No permissive mode | ❌ User-configured (pre-fix) | ⚠️ Client responsibility | N/A |
| Multi-cloud | ✅ AWS + GCP (+ Azure planned) | ⚠️ Intel SGX/TDX | ❌ Intel SGX only | N/A |
| Solves physical access | ❌ | ❌ | ❌ | ❌ |
| Deployment simplicity | ❌ Complex | ✅ One-click | ⚠️ Gramine setup | ✅ npm install |

---

## Sources

- [dstack Security Update — Attestation Pipeline Hardening (Phala, Feb 2026)](https://phala.com/posts/dstack-security-update-attestation-pipeline-hardening)
- [Mind the Gap — Where TEE Attestations Fall Short (Flashbots)](https://writings.flashbots.net/mind-the-gap-tee-poc)
- [Verifiable MCP SEP-2133](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3354)
- [attestable-mcp-server (RA-TLS)](https://github.com/kontext-dev/attestable-mcp-server)
- [Phala dstack whitepaper](https://docs.phala.com/dstack/design-documents/whitepaper)
- [MCP Security Statistics 2026 — 82% vulnerable to path traversal, 8.5% using OAuth](https://www.practical-devsecops.com/mcp-security-statistics-2026-report/)
- [NSA/CISA MCP Security Design Guidance (June 2026)](https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF)
