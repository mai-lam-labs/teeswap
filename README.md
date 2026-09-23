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
| `teeswap_quote` | Quote a transfer funded by deposit: inputs, outputs, estimated gas, plan |
| `teeswap_accept` | Accept a deposit-funded quote; returns deposit addresses |
| `teeswap_quote_x402` | Quote a transfer funded by an x402 payment (one token input) |
| `teeswap_accept_x402` | Pay for an x402-funded quote (x402 paid tool) and start it |
| `teeswap_status` | Overall status plus each input and output |
| `teeswap_invoice` | The full invoice, including the work log |

Invoices are also served at `/invoice/<id>.html` and `/invoice/<id>.json`.
The operator dashboard is at `/operator`.

## Docs

- `docs/ARCHITECTURE.md`: design and trust model
- `docs/INVOICING.md`: invoice design
- `PYTHON-CODESTYLE.md`: code conventions
