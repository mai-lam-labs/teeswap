# TEESwap

Cross-chain swap aggregator running inside a verified TEE. Every tool result
carries a Verifiable MCP attestation.

**License:** AGPL-3.0

## Quickstart

```bash
make install    # project-local uv + Python 3.14 + deps (nothing touches $HOME)
make check      # lint + format + ty + pyrefly + pytest (starts anvil and an x402 facilitator)
teeswap serve   # HTTP on 127.0.0.1:8402
teeswap stdio   # MCP over stdin/stdout
```

## Tools

Each tool is exposed over MCP (`POST /mcp`) and REST (`POST /teeswap/<name>`),
except those marked blind-only: they carry keys, so they are only available as
Verifiable MCP blind calls with an encrypted reply.

| Tool | Does |
|---|---|
| `teeswap_quote` | Quote a transfer funded by deposit: inputs, outputs, estimated gas, plan |
| `teeswap_quote_x402` | Quote a transfer funded by an x402 payment (one token input) |
| `teeswap_quote_keys` | Quote a transfer whose inputs are accounts you hold the keys to (blind-only) |
| `teeswap_accept` | Accept a deposit- or key-funded quote; returns deposit addresses |
| `teeswap_accept_x402` | Pay for an x402-funded quote (x402 paid tool) and start it |
| `teeswap_status` | Overall status plus each input and output |
| `teeswap_invoice` | The full invoice, including the work log |
| `teeswap_tools_down` | Stop: once nothing is in flight, the result is the money itself |
| `teeswap_handover` | With the tools down: the accounts, their keys and balances (blind-only) |

The quote id is the credential for the invoice: whoever holds it owns it.

Invoices are also served at `/invoice/<id>.html` and `/invoice/<id>.json`.
The operator dashboard is at `/operator`.

## Docs

- `docs/DESIGN.md`: what TEESwap is, the trust model, the decisions, and where it's going
- `docs/EXECUTION.md`: how Mai runs a job: accounts, side effects, costs, tools down
- `docs/X402.md`: taking payment by x402, and choosing facilitators
- `docs/PROTOCOLS.md`: swaps and bridges, next: custody rules, routing, tiers
- `docs/INVOICING.md`: the invoice as a record, now and planned
- `docs/LOCKBOOT.md`: building, booting and attesting on lockboot
- `docs/reference/`: digests of the external specs (x402, Verifiable MCP)
- `docs/research/`: the MCP and protocol landscape, and integration effort
- `PYTHON-CODESTYLE.md`: code conventions
