# Invoicing

The invoice is the TEE's append-only record of work performed on behalf of a user.
It is produced under attestation and serves as evidence for both the user and the
operator. Neither party can dispute the record — the TEE is the trusted bookkeeper.

## Design principles

1. **Line items are events.** The invoice is an ordered event log. There is no
   mutable status field. The current state (balance, completion, what's owed) is
   a projection over the items.

2. **One view, full transparency.** The user and operator see the same invoice.
   The operator never uses their own funds — all costs come from the user's input,
   and the operator's revenue is the fee line items. There is no hidden P&L.

3. **Record bare facts.** The invoice does not interpret cost basis, tax treatment,
   or accounting method. It records what moved, when, how much, at what rate, and
   the transaction receipts. The user's accountant interprets it.

4. **Self-contained line items.** Each item carries its own rate observations and
   timestamps so it can be independently verified and reported without context
   from other items.

## Recurring structures

These objects appear consistently wherever they're referenced, never as ad-hoc
inline fields.

### Currency

Identifies a specific token on a specific chain. USDC on Arbitrum is a different
currency from USDC on Ethereum.

```
{
  "symbol": "USDC",
  "chain": "arbitrum",
  "chain_id": 42161,
  "contract": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
  "decimals": 6
}
```

Native assets (ETH, POL) have `contract: null`.

### Transaction

A reference to an on-chain transaction.

```
{
  "hash": "0x...",
  "chain_id": 42161,
  "timestamp": "2026-09-21T09:15:32Z"
}
```

### Rate observation

An exchange rate at a point in time, with its source. The reporting currency is
operator-configured — not assumed to be USD.

```
{
  "base": "ETH",
  "quote": "GBP",
  "rate": "2105.83",
  "source": "coingecko",
  "timestamp": "2026-09-21T09:15:32Z"
}
```

The rate source is explicit because midmarket rates are not representative —
they cause phantom gains and losses. The operator configures which rate source
and reporting currency to use, and the invoice records exactly what was used,
not an idealised midmarket figure. Consumers can substitute their own rates
from the raw amounts and timestamps.

## Invoice header

```
{
  "invoice_id": "inv_...",
  "created_at": "2026-09-21T09:15:32Z",

  "operator": {
    "entity": "Mai Làm World Domination Technologies TNHH",
    "tax_id": "0123456789"
  },

  "user": null
}
```

- **operator** — the entity operating the TEE. Name and tax ID for the invoice
  issuer. Payout addresses are not here — they vary per item depending on routing.
- **user** — optional, user-provided details for their own records. Null if not
  provided. The TEE does not require user identity.

## Item types

Every item has a `type`, a `timestamp`, and the fields relevant to that type.
Items are append-only and ordered chronologically.

### `input`

User funds received by the TEE.

- `amount`, `currency` — what was received
- `tx` — the deposit transaction
- `rates` — rate observations at time of receipt

### `swap`

A conversion between currencies, executed by a protocol.

- `input_amount`, `input_currency` — what went in
- `output_amount`, `output_currency` — what came out
- `protocol` — which protocol executed it (e.g. `cow-v2`, `across-v3`)
- `effective_rate` — the actual rate achieved (output/input)
- `slippage_bps` — realised slippage vs quoted rate
- `tx` — the swap transaction
- `rates` — rate observations for both currencies

### `gas`

Execution gas consumed on-chain.

- `amount`, `currency` — gas cost denominated in the working currency
- `native_amount`, `native_currency` — gas in the chain's native token
- `tx` — the transaction that consumed the gas
- `rates` — rate observations for both the native and working currency

### `protocol_fee`

A fee charged by a third-party protocol (bridge, DEX).

- `amount`, `currency`
- `protocol` — which protocol charged it
- `rates`

### `fee`

The operator's service fee, deducted from user funds.

- `amount`, `currency`
- `rate` — the fee rate (e.g. `"0.0050"` for 0.5%)
- `computed_on` — what the fee was computed against (e.g. total input value)
- `computed_on_value` — the value it was computed against, in the fee currency
- `rates`

### `tax`

Tax on the operator's fee (e.g. VAT).

- `amount`, `currency`
- `rate` — the tax rate (e.g. `"0.25"` for 25%)
- `on` — references the fee item
- `rates`

### `output`

User funds delivered to the recipient.

- `amount`, `currency` — what was delivered
- `recipient` — the destination address
- `tx` — the delivery transaction
- `rates`

### `operator_payout`

Fee + tax disbursed to the operator's address.

- `amount`, `currency`
- `to_address` — the operator's payout address (selected by routing)
- `tx` — the payout transaction
- `transfer_gas` — gas cost of the payout transfer itself (operator expense)
- `transfer_gas_currency`
- `rates`

## Fee structure

- The operator fee is a configurable percentage of the total input value.
- VAT (configurable rate) is applied on top of the fee.
- Fee + VAT are deducted from the output side, not the input side.
- The payout address is chosen as part of the routing decision — the operator
  configures acceptable addresses per chain/currency, and the router picks one
  that minimises transfer cost.
- Gas cost of the operator payout transfer is an operator expense, recorded on
  the payout item.

## Rate source configuration

The operator configures:

- **Reporting currency** — the fiat currency for rate observations (GBP, EUR, USD, etc.)
- **Rate source** — where rates come from (e.g. CoinGecko, a specific exchange API)
- **Rate timing** — rates are captured at the timestamp of each item, not at
  invoice creation or settlement time

The invoice records the configured source and the actual rate used. It does not
apply midmarket averaging or any adjustment. Consumers who need a different rate
basis can recompute from the raw on-chain amounts and timestamps.

## Accounting use

**For the user:** Each item with a rate observation is a taxable event or deductible
expense depending on jurisdiction. The invoice provides the raw facts — amounts,
currencies, rates, timestamps, tx receipts — for the user's tax software or
accountant to interpret under their local rules.

**For the operator:** The fee items are revenue. The VAT items are VAT payable. The
operator payout transfer gas is an allowable expense. The operator tallies invoices
daily to compute VAT payable, allowable expenses, and taxable profit. Hosting costs
(AWS, etc.) and salary are accounted separately — they are not on the invoice.

## Storage and format

- **Canonical form:** JSON, produced under TEE attestation.
- **Presentation form:** PDF, generated from the JSON for human consumption.
- **Persistence:** Invoices are written to the encrypted `/data` partition
  (dm-crypt, TPM-bound keys). They survive reboots but are inaccessible if the
  enclave measurements change.
