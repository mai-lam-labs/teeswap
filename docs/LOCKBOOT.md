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
    "operator": {"legal_name": "...", "extra": "..."},
    "facilitators": [{"url": "https://facilitator.example"}]
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

## Build

Builds run in Docker, so no Rust toolchain is needed on the host. The lockboot
images (`lockboot:erofs-builder` and friends) are built from the lockboot repo.

- `make bundle-<arch>` builds TEESwap as a self-contained Python bundle
  (`Dockerfile.bundle`: musl Python and the dependencies, stripped).
- `make payload-<arch>` packages it: the container filesystem becomes an erofs
  image with dm-verity, appended to the generic lockboot stage2 loader
  (`dist/<arch>/bootstrap`), giving `dist/<arch>/stage2`.
- The result is signed with TEESwap's release key
  (`lockboot-deploy sign --domain lockboot.v1.stage2.payload`).

## Boot: `teeswap container-init`

stage2 runs `/init` as PID 1, which runs `teeswap container-init`:

1. **Reads the configuration from stdin** (the full user-data JSON, in a memfd)
   and takes the `teeswap` key.
2. **Derives the keys from the TPM**, bound to the PCRs: the Ed25519 signing key,
   the X25519 HPKE key, and the EVM root key the job accounts come from. The same
   code and configuration give the same keys on every boot.
3. **Attests**: `vaportpm-attest` with a nonce committing to both public keys, a
   timestamp, and the signing key's signature over the timestamp (proof the key
   is live, not replayed from an earlier boot):

   ```
   nonce = SHA-256(signing_public_key || hpke_public_key || timestamp || ed25519_sig(timestamp))
   ```

4. **Drops privileges** to `nobody`, and re-executes itself as `teeswap serve`,
   passing the key material over an inherited pipe: never on disk, never in
   arguments or the environment.

## Filesystem at runtime

| Path | Type | Survives reboot? | Use |
|---|---|---|---|
| `/` | overlay (erofs + tmpfs) | No | Immutable code, ephemeral writes |
| `/data` | ext4 on dm-crypt | Yes | Durable job state (planned) |
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

## Open questions

- [ ] Durable state on `/data`: its format, and recovering jobs from it at boot.
- [ ] A configuration change changes PCR 15, and so every derived key: jobs still
  running when the configuration changes can't be reached afterwards. How an
  operator changes configuration safely (drain first, or hand over).
