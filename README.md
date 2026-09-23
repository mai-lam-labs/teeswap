# TEESwap

Cross-chain swap aggregator running inside a verified TEE. Every tool result
carries a SEP-2133 Verifiable MCP attestation.

**License:** AGPL-3.0

## Quickstart

```bash
make install    # project-local uv + Python 3.14 + deps (nothing touches $HOME)
make check      # lint + format + ty + pyrefly + pytest (starts a local anvil)
teeswap serve   # HTTP on 127.0.0.1:8402
teeswap stdio   # MCP over stdin/stdout
```

## Tools

Each tool is exposed over MCP (`POST /mcp`) and REST (`POST /teeswap/<name>`).

| Tool | Does |
|---|---|
| `teeswap_quote` | Quote a transfer: inputs, outputs, estimated gas and fee |
| `teeswap_accept` | Accept a quote; returns deposit addresses |
| `teeswap_status` | Overall status plus each input and output |
| `teeswap_invoice` | The full invoice, including the work log |

Invoices are also served at `/invoice/<id>.html` and `/invoice/<id>.json`.
The operator dashboard is at `/operator`.

## Docs

- `docs/ARCHITECTURE.md`: design and trust model
- `docs/INVOICING.md`: invoice design
- `PYTHON-CODESTYLE.md`: code conventions
