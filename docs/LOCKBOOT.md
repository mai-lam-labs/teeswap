# TEESwap × lockboot integration

How TEESwap runs as a lockboot stage2 consumer — the first third-party app on the chain.

## Boot chain

```
stage0 (UEFI, Secure Boot)
  → stage1 (UKI, admits stage2 by ed25519/sha256, measures into PCR14)
    → stage2 loader (generic, payload-agnostic, lockboot-signed)
      → TEESwap container image (appended erofs, dm-verity'd)
        → /init (PID 1, reads config from stdin)
```

## Trust domains

Two independent signing keys, two trust domains:

| Artifact | Signed by | Admitted by |
|---|---|---|
| stage1 UKI (`linux.efi`) | lockboot release key | stage0 (ed25519 or sha256 pin) |
| stage2 binary (loader + TEESwap erofs) | **TEESwap release key** | stage1 (manifest hop, ed25519) |

The lockboot-signed loader is the generic audited piece. TEESwap appends its
container image to it via `erofs-builder`, producing a new combined binary.
That combined binary is signed with TEESwap's own release key and admitted via
a signed manifest.

## Deployment artifacts

### User-data (instance cloud-init / metadata)

```json
{
  "_stage1": {
    "x86_64": {
      "payload": {
        "url": "https://cdn.example/lockboot/linux.efi",
        "ed25519": "LOCKBOOT_RELEASE_PUBKEY_B64"
      }
    }
  },
  "_stage2": {
    "x86_64": {
      "manifest": {
        "url": "https://cdn.example/teeswap/manifest.json",
        "ed25519": "TEESWAP_RELEASE_PUBKEY_B64"
      }
    }
  },
  "teeswap": {
    "bind": "0.0.0.0:8402",
    "payment_address": "0x..."
  }
}
```

### Signed manifest (`manifest.json` + `manifest.json.sig`)

```json
{
  "_stage2": {
    "x86_64": {
      "payload": {
        "url": ["https://cdn1.example/teeswap/stage2", "https://cdn2.example/teeswap/stage2"],
        "sha256": "abc123..."
      }
    }
  }
}
```

The manifest is signed with TEESwap's release key (domain-separated ed25519,
`lockboot.v1.stage2.manifest`). stage1 verifies it, deep-merges into user-data
(the `teeswap` config block survives), then fetches and admits the payload by
sha256.

Updates: rebuild the image, re-sign the manifest, push. No user-data change
needed — the ed25519 key is pinned, not the binary hash.

## Build pipeline

All builds run inside Docker containers — no Rust toolchain on the host.

### Prerequisites (already on this machine)

```
lockboot:build          — Rust + musl cross-compilation
lockboot:erofs-builder  — mkfs.erofs + veritysetup + zip (the packaging CLI)
lockboot:harness        — QEMU/swtpm test harness
```

If missing, build from the lockboot repo:
```bash
cd /home/user/Projects/lockboot/stage2
make docker-build-base docker-build-erofs
```

### Build the TEESwap stage2 binary

```bash
# 1. Build the TEESwap container image (must have /init)
docker build -t teeswap .

# 2. Get the lockboot loader (pre-built or from lockboot repo)
#    The loader is generic — same binary for any payload.
LOADER=/home/user/Projects/lockboot/stage2/target/x86_64-unknown-linux-musl/release/bootstrap

# 3. Package: export container → erofs → append to loader → stage2 binary
docker export "$(docker create --rm teeswap)" \
  | docker run --rm -i \
      -v "$LOADER:/bootstrap:ro" \
      lockboot:erofs-builder --bootstrap /bootstrap - - > build/stage2

# 4. Sign with TEESwap's release key (lockboot-deploy)
lockboot-deploy sign \
  --domain lockboot.v1.stage2.payload \
  --key teeswap-release.pem \
  --in build/stage2 \
  --out build/stage2.sig
```

## What `/init` does

stage2 does `execv("/init")` as PID 1 in the overlay root. Our `/init` must:

1. **Read config from stdin** (fd 0) — a seekable memfd containing the full
   user-data JSON. Parse the `teeswap` key for app config.

2. **Generate key material** — Ed25519 signing key + X25519 HPKE keypair.
   These are ephemeral to each boot (not persisted to `/data`).

3. **Attest the keys** — call `vaportpm-attest` with a nonce that commits to
   both public keys + a timestamp + liveness signatures from both keys. This
   proves "these keys were generated inside this TEE with this code and config."

4. **Start the server** — bind to the address from config, serve MCP + REST.

5. **Handle PID 1 duties** — SIGTERM for graceful shutdown, SIGCHLD reaping.

## Filesystem at runtime

| Path | Type | Survives reboot? | Use |
|---|---|---|---|
| `/` | overlay (erofs + tmpfs) | No | Immutable code, ephemeral writes |
| `/data` | ext4 on dm-crypt | Yes | Invoice store, persistent state |
| `/run/lockboot/stage1.attest` | file | No | Stage1 attestation |
| `/run/lockboot/stage2.attest` | file | No | Stage2 config digest |
| stdin (fd 0) | memfd | No | User-data JSON config |

## PCR binding

| PCR | Contents | Implication |
|---|---|---|
| 0–4, 6–9, 11–23 | Firmware, stage0, stage1 | Bound to disk encryption key |
| 14 | SHA-256 of entire stage2 binary (loader + TEESwap erofs) | Code identity — different image = different attestation |
| 15 | SHA-256 of user-data JSON | Config identity — any config change wipes `/data` |
| 5, 10 | Excluded (GPT, IMA) | See PCR-BINDING.md |

## Attestation flow (boot time)

```
generate ed25519 signing keypair
generate x25519 HPKE keypair
timestamp = current unix time

both keys sign the timestamp (liveness proof)

nonce = SHA-256(
  signing_public_key
  || hpke_public_key
  || timestamp
  || ed25519_signature(timestamp)
  || x25519... (TBD: X25519 is not a signing key, need to reconsider)
)

attestation = vaportpm-attest(nonce)
```

**Open question:** X25519 is a DH key, not a signing key — it can't sign the
timestamp. Options:
- Use only the Ed25519 signature in the nonce (HPKE key is still committed by
  inclusion in the hash)
- Generate an Ed25519 key for HPKE signing and use X25519 only for DH
- Derive the X25519 key from the Ed25519 key (RFC 7748 birational map)

The nonce commits to both public keys regardless — the liveness signature just
additionally proves the signing key is live (not replayed from a previous boot).

## Docker dev environment (no TEE)

For local development without a real TPM or lockboot chain:

```bash
# TODO: Dockerfile that runs TEESwap with:
# - Ephemeral Ed25519 key (no attestation)
# - Config from a mounted JSON file (simulates stdin)
# - SQLite in a volume (simulates /data)
# - No dm-verity, no overlay — just runs the Python app directly
```

## Open questions

- [ ] `/init` implementation: shell script wrapping Python, or a small Go/Rust
  binary that sets up PID 1 duties then execs Python?
- [ ] How to bundle Python 3.14 + deps in the container image — venv? PyInstaller?
  uv-managed self-contained directory?
- [ ] Persistent state schema for `/data` — SQLite? append-only log?
- [ ] Config schema for the `teeswap` block in user-data
- [ ] X25519 liveness proof approach (see attestation flow above)
