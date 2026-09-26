# x402 (reference)

A digest of the external x402 v2 spec and ecosystem. How TEESwap uses x402 is in
`../X402.md`.

Spec: https://docs.x402.org
Source: https://github.com/x402-foundation/x402

x402 has two transports:

- **HTTP**: the server answers `402 Payment Required` with payment requirements,
  the client retries with a signed payment in HTTP headers.
- **MCP**: payment travels inside the JSON-RPC messages of a `tools/call`; no
  HTTP status codes or headers are involved.

Either way, the client signs a payment authorization and a facilitator settles
it on-chain.

## MCP transport

Spec: [`specs/transports-v2/mcp.md`](https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/mcp.md)
(x402 v2).

### Flow

```
Client                                           Server (TEE)
  │  tools/call  (no payment)                        │
  │ ───────────────────────────────────────────────► │
  │  result: isError: true                           │
  │          structuredContent: PaymentRequired      │
  │          content[0].text:  same, JSON-encoded    │
  │ ◄─────────────────────────────────────────────── │
  │                                                  │
  │  tools/call  (same call)                         │
  │    params._meta["x402/payment"]: PaymentPayload  │
  │ ───────────────────────────────────────────────► │
  │                               verify → execute → settle (facilitator)
  │  result: the tool's content                      │
  │    _meta["x402/payment-response"]: settlement    │
  │ ◄─────────────────────────────────────────────── │
```

Payment-required and settlement failure are **tool results** (`isError: true`),
not JSON-RPC errors.

### 1. Payment required

Servers MUST return a tool result with `isError: true` containing the
`PaymentRequired` data, and MUST provide it in both formats:

- `structuredContent` (REQUIRED): the `PaymentRequired` object
- `content[0].text` (REQUIRED): the same `PaymentRequired`, JSON-encoded

Clients SHOULD prefer `structuredContent` when available, falling back to
parsing `content[0].text`.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "isError": true,
    "structuredContent": {
      "x402Version": 2,
      "error": "Payment required to access this resource",
      "resource": {
        "url": "mcp://tool/financial_analysis",
        "description": "Advanced financial analysis tool",
        "mimeType": "application/json"
      },
      "accepts": [
        {
          "scheme": "exact",
          "network": "eip155:84532",
          "amount": "10000",
          "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
          "payTo": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
          "maxTimeoutSeconds": 60,
          "extra": { "name": "USDC", "version": "2" }
        }
      ]
    },
    "content": [
      { "type": "text", "text": "{\"x402Version\":2,\"error\":\"Payment required to access this resource\", …}" }
    ]
  }
}
```

### 2. Paying

The client repeats the tool call with the payment at `params._meta["x402/payment"]`:
the `resource`, the chosen entry from `accepts` as `accepted`, and the signed `payload`.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "financial_analysis",
    "arguments": { "ticker": "AAPL", "analysis_type": "deep" },
    "_meta": {
      "x402/payment": {
        "x402Version": 2,
        "resource": {
          "url": "mcp://tool/financial_analysis",
          "description": "Advanced financial analysis tool",
          "mimeType": "application/json"
        },
        "accepted": {
          "scheme": "exact",
          "network": "eip155:84532",
          "amount": "10000",
          "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
          "payTo": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
          "maxTimeoutSeconds": 60,
          "extra": { "name": "USDC", "version": "2" }
        },
        "payload": {
          "signature": "0x2d6a…571c",
          "authorization": {
            "from": "0x857b06519E91e3A54538791bDbb0E22373e36b66",
            "to": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            "value": "10000",
            "validAfter": "1740672089",
            "validBefore": "1740672154",
            "nonce": "0xf374…3480"
          }
        }
      }
    }
  }
}
```

### 3. Settlement

On success the result is the tool's normal content, with the settlement at
`_meta["x402/payment-response"]`:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{ "type": "text", "text": "Financial analysis for AAPL: …" }],
    "_meta": {
      "x402/payment-response": {
        "success": true,
        "transaction": "0x1234…cdef",
        "network": "eip155:84532",
        "payer": "0x857b06519E91e3A54538791bDbb0E22373e36b66"
      }
    }
  }
}
```

### Settlement failure

The server returns a tool result with `isError: true` in the same format as
payment required, with `error` describing the failure (e.g. `"Settlement failed"`).
If settlement fails after the tool has already executed, the server does not
return the tool's content, only the payment error.

## HTTP transport

Spec: [`specs/transports-v2/http.md`](https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/http.md).
Each header carries base64-encoded JSON.

| | Header |
|---|---|
| Payment required | `402` status, `PAYMENT-REQUIRED`: the `PaymentRequired` object |
| Payment in | `PAYMENT-SIGNATURE`: the `PaymentPayload` |
| Settlement out | `PAYMENT-RESPONSE`: the settlement result |

## Facilitators

A facilitator is a third-party service that verifies payment signatures,
broadcasts the settlement on-chain (paying the gas), and returns the settlement
result. It holds no funds: it relays signed authorizations.

### API

| Endpoint | Purpose |
|---|---|
| `GET /supported` | Returns supported schemes, networks, and extensions |
| `POST /verify` | Verify a payment payload is valid |
| `POST /settle` | Submit payment on-chain, wait for confirmation |

## Payment schemes

| Scheme | Description | Use case |
|---|---|---|
| `exact` | Fixed amount transfer. Client signs authorization for exactly X. | "Pay 10 USDC for this operation" |
| `upto` | Max-cap authorization. Client authorizes up to X, facilitator settles actual amount. | "Swap roughly $1000 worth — exact amount depends on execution" |
| `batch-settlement` | Client deposits into escrow, signs off-chain vouchers per request, facilitator redeems in batches. | High-frequency micropayments |

## Networks

Networks are identified by CAIP-2 format:

| Ecosystem | Format | Example |
|---|---|---|
| EVM | `eip155:<chainId>` | `eip155:8453` (Base), `eip155:42161` (Arbitrum) |
| Solana | `solana:<genesisHash>` | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` (mainnet) |
| Stellar | `stellar:<network>` | `stellar:pubnet` |
| Algorand | `algorand:<reference>` | `algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73k` |

Some facilitators also report human-readable aliases (`base`, `polygon`,
`solana`) alongside the CAIP-2 identifiers. Both refer to the same network.

## Token transfer methods (EVM)

On EVM, three methods exist for moving tokens from the user to the TEE. The
method determines which tokens are supported and whether the user needs gas.

### EIP-3009: `transferWithAuthorization`

- User signs a single off-chain authorization
- Facilitator calls `transferWithAuthorization()` on the token contract
- User needs no gas — the facilitator broadcasts
- **Only works with tokens that implement EIP-3009** (USDC, EURC)
- Preferred path when available

### Permit2: `permitWitnessTransferFrom`

- User must first `approve()` the Permit2 contract for their token (one-time)
- Then user signs an off-chain Permit2 transfer authorization
- Facilitator calls Permit2's `permitWitnessTransferFrom()`
- **Works with any ERC-20** — universal fallback
- The one-time approval requires gas (see extensions below)

Permit2 contract: `0x000000000022D473030F116dDEE9F6B43aC78BA3` (same on all EVM chains)

x402 proxy contracts (CREATE2 deterministic, same on all EVM chains):
- `x402ExactPermit2Proxy`: `0x402085c248eea27d92e8b30b2c58ed07f9e20001`
- `x402UptoPermit2Proxy`: `0x4020a4f3b7b90cca423b9fabcc0ce57c6c240002`

### ERC-7710: Smart account delegation

- For smart contract accounts with delegation support
- Not yet widely adopted

## Token transfer methods (non-EVM)

| Ecosystem | Method | Notes |
|---|---|---|
| Solana | SPL Transfer | Native SPL/Token-2022 token transfer |
| Stellar | Soroban transfer | SEP-41 tokens |
| Algorand | ASA transfer (axfer) | Algorand Standard Assets |

## Extensions

Extensions are optional capabilities a facilitator can advertise. They affect
what tokens and flows are available.

### `eip2612GasSponsoring`

Sponsors the one-time Permit2 approval for EIP-2612 tokens.

- Token must implement EIP-2612 `permit()` (DAI, many modern ERC-20s)
- User signs a `permit()` off-chain, facilitator broadcasts it gaslessly
- After the permit, the normal Permit2 transfer flow proceeds
- User never needs gas

### `erc20ApprovalGasSponsoring`

Sponsors the one-time Permit2 approval for any ERC-20, even without EIP-2612.

- User signs a raw `approve(Permit2, amount)` transaction offline
- Facilitator sends ETH to the user's address to cover gas
- Facilitator broadcasts the user's pre-signed approval transaction
- After the approval, the normal Permit2 transfer flow proceeds
- Most expensive path — two extra transactions (gas funding + approval)
- **Works with every ERC-20**

### `bazaar`

API discovery and cataloging. Lets facilitators index what x402-protected
resources are available. Not relevant to token support — it's a discovery
protocol for building API marketplaces.

### `durable-evidence`

Records payment evidence on-chain permanently. Seen on UltravioletaDAO.

### `builder-code`

Developer tooling extension. Seen on Heurist.

## Top tokens by transfer method

### EIP-3009 (`transferWithAuthorization`)

Only Circle tokens implement this natively. Smallest set, best UX.

| Token | Chains |
|---|---|
| USDC | Ethereum, Base, Arbitrum, Polygon, Avalanche, Optimism |
| EURC | Ethereum, Base, Avalanche |

### EIP-2612 (`permit`)

Most tokens deployed since ~2021 via OpenZeppelin include ERC20Permit.

| Token | Notes |
|---|---|
| USDC | Also has EIP-3009 |
| DAI / USDS | MakerDAO — early adopter |
| WETH | Wrapped Ether |
| UNI | Uniswap governance |
| AAVE | Aave governance |
| ARB | Arbitrum governance |
| OP | Optimism governance |
| LDO, stETH, wstETH | Lido |

### Permit2 only (no native permit support)

These need the one-time `approve()` to Permit2. The user needs gas for that
approval unless the facilitator has `erc20ApprovalGasSponsoring`.

| Token | Notes |
|---|---|
| USDT | Highest volume stablecoin — no EIP-3009, no EIP-2612 |
| WBTC | Wrapped Bitcoin |
| LINK | Chainlink |
| SHIB, PEPE, etc. | Legacy/meme tokens |

USDT is the critical gap — highest volume, worst UX without gas sponsoring.

### Solana (SPL)

All SPL tokens are transferable via the same mechanism — no permit/approval
distinction. If a facilitator supports Solana, it supports every SPL token.

| Token | Mint |
|---|---|
| USDC | EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v |
| USDT | Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB |
| SOL | Native (not SPL — wrapped as So11111111111111111111111111111111111111112) |
| JUP | JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN |
| JTO | jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL |
| RAY | 4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R |
| PYTH | HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3 |

## Token support matrix

The combination of transfer method + facilitator extensions determines what
tokens a facilitator can handle and whether the user needs gas:

| Token type | Method | Gas needed? | Extension required |
|---|---|---|---|
| USDC, EURC | EIP-3009 | No | None |
| EIP-2612 tokens (DAI, WETH, etc.) | Permit2 + permit | No | `eip2612GasSponsoring` |
| Any ERC-20 (incl. USDT, WBTC) | Permit2 + approve | No (sponsored) | `erc20ApprovalGasSponsoring` |
| Any ERC-20 | Permit2 + approve | Yes (user pays) | None |
| SPL tokens (Solana) | SPL Transfer | N/A | None |
