# MCP Blockchain/DeFi Landscape Research

**Date:** 2026-09-16
**Purpose:** survey of existing MCP servers for on-chain execution, swaps, bridges, and wallets — what exists, what tools they expose, what patterns to adopt or avoid for TEESwap.

---

## Execution & Swap Servers

### deBridge MCP
- **Repo:** https://github.com/debridge-finance/debridge-mcp
- **What it is:** cross-chain swap execution via deBridge DLN protocol. First open-source MCP server for cross-chain DeFi (launched Feb 2026).
- **Chains:** 28 networks (EVM + Solana)
- **Transport:** stdio and HTTP streaming. npm package proxies all requests to https://agents.debridge.com/mcp
- **Open source:** yes (MIT)

**Tools:**

| Tool | What it does |
|---|---|
| `get_instructions` | Returns full usage guide — workflow, tips, all tool docs |
| `search_tokens` | Look up tokens by name/symbol/address, filter by chain ID |
| `get_supported_chains` | List all supported chains with chain IDs |
| `create_tx` | Build a cross-chain swap tx — source/dest chains, tokens, amounts, recipient. Returns tx data ready to sign |
| `transaction_same_chain_swap` | Same-chain swap variant |

**Workflow pattern:** resolve chains → resolve tokens → create tx → generate dApp URL for user to execute. Agent does NOT sign — it generates a link the user opens.

**Relevant to TEESwap:** closest existing model. But locked to deBridge as backend. TEESwap would expose similar tools but route through multiple backends.

---

### Jupiter MCP (Solana)
- **Repos:** https://github.com/kukapay/jupiter-mcp, https://github.com/araa47/jupiter-mcp
- **What it is:** Solana token swaps via Jupiter Ultra API (DEX aggregator)
- **Chains:** Solana only
- **Open source:** yes

**Tools:**

| Tool | What it does |
|---|---|
| `get_quote` | Get swap quote from Jupiter Ultra API (routing + RFQ combined) |
| `execute_swap` | Execute swap — handles slippage, priority fees, tx landing |
| `get_token_info` | Token metadata lookup |

**Key detail:** requires a Solana private key (base58) — the MCP server signs and submits directly. This is the "server holds key" model, no user approval step.

**Relevant to TEESwap:** shows the pattern for Solana integration. TEESwap's gVisor container would hold the scoped key similarly but with isolation.

---

### Haiku MCP
- **Repo:** https://github.com/Haiku-Trading/haiku-mcp-server
- **What it is:** multi-chain DeFi execution — swaps, lending, vaults, LP, bridges across 22 chains and 45+ protocols in a single transaction
- **Chains:** 22 EVM chains
- **Open source:** yes

**Tools (7):**

| Tool | What it does |
|---|---|
| Token discovery | Search for tokens across supported chains |
| Balance checking | Get wallet balances across all chains |
| Trading quotes | Get quotes for swaps and portfolio rebalancing |
| Transaction building | Convert quotes to unsigned EVM transactions |
| (+ 3 more undocumented) | |

**Key detail:** returns unsigned transactions — user/wallet signs separately. Read-only server, no key material.

**Relevant to TEESwap:** the multi-protocol aggregation (45+ protocols, single tx) is ambitious. Their quote→unsigned-tx pattern is clean.

---

### Uniswap Trader MCP
- **What it is:** automated token swaps on Uniswap across multiple networks
- **Open source:** yes
- **Relevant to TEESwap:** same-chain swap primitive, EVM focused.

---

## Bridge & Cross-Chain Servers

### Relay Protocol MCP
- **Repo:** https://github.com/relayprotocol/relay-mcp (or community forks)
- **What it is:** cross-chain bridge and swap quotes via Relay Protocol
- **Chains:** major EVM + Bitcoin + Solana (85+ chains in Relay)
- **Open source:** yes
- **Tools: 8**

| Tool | What it does |
|---|---|
| List chains | Supported networks, filterable by VM type (EVM/SVM) |
| Search tokens | Token lookup across chains by name/symbol, filter by chain + verified status |
| Bridge quote | Quote for bridging same token between chains (e.g., ETH→ETH), with fees and time |
| Swap quote | Quote for swapping different tokens, optionally cross-chain (e.g., ETH→USDC) |
| Get bridge steps | Unsigned tx steps (approvals + deposits) for a bridge |
| Get swap steps | Unsigned tx steps for a swap |
| Transaction status | Poll status of in-progress/completed tx by request ID |
| (+ 1 more) | |

**Key detail:** all tools are read-only — returns quotes, unsigned txs, and status. Does NOT sign or broadcast. Execution requires separate wallet integration.

**Relevant to TEESwap:** clean separation of quoting vs execution. TEESwap could adopt the same tool shape for quote/status tools, but handle signing internally via TEE.

---

### Chainflip Broker MCP
- **What it is:** cross-chain swaps via Chainflip protocol (native BTC, ETH, SOL)
- **Open source:** yes, with hosted endpoint
- **Relevant to TEESwap:** Chainflip is in our Tier 3 (TRON exploit, Sep 2026) — probably not an integration target but the MCP tool pattern is informative.

---

## Wallet & Key Management Servers

### Coinbase Payments MCP + Base MCP
- **Repo:** https://github.com/coinbase/payments-mcp, https://github.com/coinbase/agentkit
- **What it is:** two-layer stack. Payments MCP = wallet + onramp + x402 payments. Base MCP = DeFi protocol interactions.
- **Chains:** Base primarily, EVM broadly
- **Auth:** OAuth 2.1 — user grants scoped permissions to the agent via Base Account

**Payments MCP capabilities:**
- Wallet creation and management
- Balance checking
- Token transfers
- Onramp (fiat → crypto)
- x402 payment settlement

**Base MCP capabilities (DeFi):**
- Swaps (Uniswap, Aerodrome)
- Lending (Morpho, Moonwell)
- LP management
- Perpetuals trading (Avantis)
- Token launches (Virtuals)

**AgentKit action providers:**
- `erc20_transfer`, `swap`, `smart_contract_call`, `nft_mint`, `pyth_price_feed`, `morpho_lend`

**Security model:** "stored requests" — agent proposes a transaction, user approves and signs via Base Account. Agent CANNOT sign or broadcast autonomously.

**Relevant to TEESwap:** the agent-proposes-user-approves pattern is the opposite of TEESwap's model (TEE signs autonomously). But the MCP tool shape and DeFi verb taxonomy are directly useful.

---

### VaultPilot MCP
- **Repo:** https://github.com/agenthill/vaultpilot-mcp
- **What it is:** hardware-verified DeFi for AI agents. Agent proposes, user approves on Ledger.
- **Design philosophy:** "designed for when the AI / MCP server / host computer can be compromised"
- **Open source:** yes

**Supported protocols:**

| Category | Protocols |
|---|---|
| Lending | Aave V3, Compound V3, Morpho Blue |
| DEX/Swap | Uniswap V3 (swap + LP), Curve, Jupiter v6 (Solana) |
| Staking | Lido, EigenLayer, Rocket Pool, Marinade, Jito |
| Bridge | LiFi (EVM + EVM↔Solana + TRON + BTC) |
| Multisig | Safe (Gnosis) |
| Solana DeFi | MarginFi, Kamino |
| TRON | SunSwap |

**Signing:** EVM via WalletConnect → Ledger Live. TRON/Solana/Bitcoin/Litecoin via USB HID direct to device.

**Relevant to TEESwap:** different trust model (hardware wallet vs TEE) but the protocol coverage and DeFi verb taxonomy are excellent reference. Their approach of preparing unsigned transactions is similar to Relay MCP.

---

### mcp-cryptowallet-evm (dcSpark)
- **Repo:** https://github.com/dcSpark/mcp-cryptowallet-evm
- **What it is:** EVM wallet management — creation, balance, transfers, contract interaction
- **Open source:** yes
- **Relevant to TEESwap:** basic wallet primitives, useful as reference for tool interface design.

---

## Payment & Escrow Servers

### Voidly Pay
- **What it is:** agent-to-agent payments in USDC-backed credits over HTTP 402 with real settlement on Base
- **Tools:** 28 including escrow, transfers, streams
- **Open source:** yes
- **Relevant to TEESwap:** the x402 + escrow + streaming pattern. 28 tools is a lot — shows how granular the MCP tool decomposition can get.

### Paycrow
- **What it is:** smart escrow with dispute resolution for AI agent payments on Base
- **Open source:** yes
- **Relevant to TEESwap:** if agents need to handle disputes over swap quality, Paycrow's model is interesting. Trust-informed escrow with on-chain dispute resolution.

---

## Data & Market Servers

### CryptoAPIs MCP
- **What it is:** read-only blockchain data — balances, transactions, token info across multiple chains
- **Open source:** yes
- **Relevant to TEESwap:** useful for the `data-monitor` tool (watching for deposits, fills, deliveries).

### Crypto.com Market Data MCP
- **What it is:** high-performance crypto market data (prices, volumes, etc.)
- **Relevant to TEESwap:** useful for pricing/quoting if DEX API quotes are insufficient.

---

## Patterns to Adopt

### 1. Tool shape: resolve → quote → execute → status
Every successful MCP server follows this pattern:
- **Resolve** chains and tokens first (human-readable → on-chain identifiers)
- **Quote** with full transparency (fees, time, route)
- **Execute** (or return unsigned tx for user signing)
- **Status** polling for async operations

TEESwap should follow the same flow: `teeswap_routes` → `teeswap_quote` → `teeswap_execute` → `teeswap_status`.

### 2. Read-only vs. signing authority
The landscape splits into two models:

| Model | Examples | Trust anchor |
|---|---|---|
| **Agent proposes, user signs** | Relay MCP, VaultPilot, Base MCP, Haiku | User's wallet (Ledger, Base Account, etc.) |
| **Server holds key, signs autonomously** | Jupiter MCP, deBridge (via dApp link) | Server operator |

TEESwap is a third model: **TEE holds key, signs autonomously, attestation is the trust anchor.** This is novel — no existing MCP server uses TEE-attested autonomous signing.

### 3. Don't duplicate — compose
deBridge MCP doesn't reimplement cross-chain logic — it wraps deBridge's HTTP API. Jupiter MCP wraps Jupiter's Ultra API. The MCP server is a thin translation layer between agent tool calls and existing protocol APIs.

TEESwap should do the same: MCP tools call protocol HTTP APIs (CoW orderbook, Across API, Circle Iris API) where available, and fall back to on-chain calls via `cast` only when necessary.

### 4. x402 micropayments for tool access
Multiple servers (CryptoIZ, Sol-MCP, Cerebrus Pulse) use x402 micropayments for tool access. This validates TEESwap's x402-paywalled quote endpoint.

---

## Patterns to Avoid

### 1. Key material in environment variables
Jupiter MCP takes the Solana private key as a config parameter. This is fine for personal use but wrong for a multi-user service. TEESwap's gVisor-scoped key material is the right approach.

### 2. Monolithic protocol coverage
Haiku covers 45+ protocols and 22 chains. That's impressive but creates a massive maintenance surface. TEESwap should start with 3 integrations (CoW, Across, CCTP) and add on demand.

### 3. Agent-generates-link-for-user pattern
deBridge MCP generates a dApp URL the user opens to execute. This breaks the autonomous agent flow. TEESwap should execute directly — the TEE signs and submits.

---

## Competitive positioning

| Capability | deBridge MCP | Relay MCP | Haiku MCP | Coinbase Base MCP | **TEESwap** |
|---|---|---|---|---|---|
| Cross-chain swaps | Yes (deBridge only) | Yes (Relay only) | Yes (multi-protocol) | Limited | Yes (multi-protocol) |
| Protocol-agnostic | No | No | Yes | No (Base ecosystem) | **Yes** |
| Risk-aware routing | No | No | No | No | **Yes (audit data)** |
| Autonomous signing | No (dApp link) | No (unsigned txs) | No (unsigned txs) | No (user approves) | **Yes (TEE)** |
| Per-swap isolation | No | No | No | No | **Yes (gVisor)** |
| x402 native | No | No | No | Yes (Payments MCP) | **Yes** |
| Verifiable execution | No | No | No | No | **Yes (attestation)** |

---

## Cross-reference: our audit tiers vs. existing MCP coverage

| Our protocol (Tier) | Has MCP server? | What it covers | Gap for TEESwap |
|---|---|---|---|
| **UniswapX** (Tier 1) | Uniswap Trader MCP (generic Uniswap, not UniswapX-specific) | Same-chain Uniswap swaps | Need UniswapX-specific order/fill flow |
| **CoW Protocol** (Tier 1) | **None** | — | Build wrapper around CoW HTTP orderbook API |
| **The Compact** (Tier 1) | **None** | — | Build wrapper — on-chain `cast` calls |
| **OIF Escrow** (Tier 1) | **None** | — | Build wrapper — on-chain `cast` calls |
| **Skip Go Fast** (Tier 1) | **None** | — | Build wrapper — on-chain + some API |
| **Across V3** (Tier 2) | **None** | — | Build wrapper — one `cast` call + Across API |
| **Mayan Swift** (Tier 2) | **None** | — | Build wrapper — API-driven quote + on-chain deposit |
| **CCTP V2** (Tier 2) | **Yes — 3 servers** (ArcLeap, Circle Agent Stack, Circle-MCP) | Full burn/attest/mint flow | Could study ArcLeap tool schema; build our own thin wrapper |
| **IBC/Eureka** (Tier 2) | **None** | — | Build wrapper — newer, less documented |
| | | | |
| **deBridge DLN** (Tier 3) | **Yes** — deBridge MCP | Full cross-chain swap via DLN | Available but we rated deBridge Tier 3 (no expiry, validator dependency) |
| **Relay.link** (Tier 3ish) | **Yes** — Relay MCP | Bridge/swap quotes + unsigned tx steps | Available, 8 well-designed tools |
| **Chainflip** (Excluded) | **Yes** — Chainflip Broker MCP | Cross-chain native asset swaps | Excluded (TRON exploit, Sep 2026) |
| **Symbiosis** (Excluded) | **Yes** — Symbiosis MCP | 50+ chain swaps | Excluded (BTC bridge exploit, Sep 2026) |

**Key finding:** the protocols with the best revocability properties (our Tier 1) have **zero** MCP coverage. The protocols with MCP servers (deBridge, Relay, Chainflip, Symbiosis) are mostly our Tier 3 or excluded. TEESwap would be building the first MCP interfaces for CoW, Across, The Compact, OIF, Skip Go Fast, and Mayan Swift — a genuine gap in the ecosystem.

CCTP is the exception — three MCP servers exist for a Tier 2 protocol. ArcLeap MCP in particular has a clean tool schema worth studying.

## Tool schema patterns from source review

### Relay MCP — cleanest read-only design (source reviewed)
- **21 tools** covering discovery, quoting, execution steps, and monitoring
- Every tool uses **Zod schemas** for typed input validation
- Human-readable chain/token resolution built in (accepts "ethereum" or 1, "USDC" or address)
- `includeSteps` flag — agent can request unsigned tx steps or omit them to save tokens
- Returns structured JSON + human summary + deeplink URL as fallback
- **Key pattern:** `resource_link` content type for "open in browser" fallback alongside structured data
- Source: `relay-mcp/src/tools/get-swap-quote.ts`

### Jupiter MCP — minimal autonomous signing (source reviewed)
- **2 tools:** `get-ultra-order` + `execute-ultra-order`
- Private key loaded from env var — server signs and submits directly
- Ultra API handles routing, slippage, priority fees, and tx landing
- Quote returns `requestId` + `transaction` (base64); execute signs and posts
- **Key pattern:** two-step quote→execute where the quote ID links them. Minimal tool count.
- Source: `jupiter-mcp/index.js`

### VaultPilot — most mature swap schema (source reviewed)
- **~40+ modules** covering swap, bridge, lending, staking, LP, NFT, UTXO chains, Safe multisig
- Swap schema is the most sophisticated found:
  - `amount` in human-readable decimals (not wei) — tool resolves decimals internally
  - `amountSide: "from" | "to"` — exact-in or exact-out
  - `slippageBps` with hard cap at 500 (5%) + `acknowledgeHighSlippage` flag
  - `exchanges` / `bridges` allowlists and `excludeExchanges` / `excludeBridges` blocklists
  - `order: "RECOMMENDED" | "FASTEST" | "CHEAPEST" | "SAFEST"` route ranking
  - **Prompt injection defense:** cross-chain `toAddress` to different EVM wallet is REFUSED because a rogue agent controls all tool args — the convenience is dropped rather than gated on an agent-settable flag (#798)
  - Security-aware: refuses blind-signed typed data, pre-flight checks for gas/pause/caps
- Routes through LiFi and 1inch under the hood
- **Key patterns:** human-readable amounts, safety caps with acknowledgement flags, prompt-injection-aware recipient validation, route ranking preference
- Source: `vaultpilot-mcp/src/modules/swap/schemas.ts`

### Circle Agent Stack MCP — x402 + wallet (source reviewed)
- **4 tool groups:** wallet, policy, transfer, x402
- `circle_x402_pay` tool: takes wallet_id, endpoint URL, max_price_usdc, method, body — pays an x402 endpoint in one call
- `circle_transfer_usdc`: wallet_id, to_address, amount_usdc, memo
- Thin wrappers around Circle's CLI tool (`circleCLI`)
- **Key pattern:** `max_price_usdc` as safety cap on x402 payments. The tool refuses if the endpoint quotes higher.
- Source: `circle-agent-stack-mcp/src/tools/x402.ts`, `transfer.ts`

## Key repos to study

| Repo | Why |
|---|---|
| https://github.com/debridge-finance/debridge-mcp | Closest to TEESwap's tool shape. Study the tool schemas and workflow pattern. |
| https://github.com/relayprotocol/relay-mcp | Clean read-only quote/status tools. Good separation of concerns. |
| https://github.com/agenthill/vaultpilot-mcp | Broadest protocol coverage (Aave, Compound, Morpho, Uniswap, Curve, Lido, LiFi, Jupiter, etc.). Study the DeFi verb taxonomy. |
| https://github.com/coinbase/agentkit | Framework-agnostic action providers. Study the pluggable skill model. |
| https://github.com/coinbase/payments-mcp | x402 + wallet integration. Study the MCP+x402 composition. |
| https://github.com/Haiku-Trading/haiku-mcp-server | Multi-protocol aggregation pattern. Study how they compose 45+ protocols behind 7 tools. |
| https://github.com/kukapay/jupiter-mcp | Minimal Solana swap MCP. Study the Jupiter Ultra API integration. |
| https://github.com/TensorBlock/awesome-mcp-servers/blob/main/docs/finance--crypto.md | Curated list of all crypto MCP servers. |
