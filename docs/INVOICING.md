# Invoicing

The invoice is the TEE's record of what it did with a user's funds. It's produced
under attestation and is the evidence for both sides: the owner reads it by the
invoice id, the operator by its `ref`. Neither can dispute it; the TEE keeps the
books.

## Principles

1. **Record bare facts.** What moved, when, how much, and the transactions that
   prove it. The invoice doesn't interpret cost basis, tax treatment or
   accounting method; the user's accountant does.
2. **An append-only record.** Funds only change position through movements,
   added in order and never edited. Totals and status are read from them.
3. **Self-contained items.** Each item carries what's needed to check and report
   it on its own: amounts, the token and chain, the transaction, the time.
4. **No hidden P&L.** The operator never puts in money: every cost is paid from
   the user's funds and shows on the invoice, and the operator's income will be
   its fee items (below).

## What it records today

- **Movements**, each moving an amount of a token between places with a custody
  state (held, in flight, delivered, consumed, released): an input arriving, funds
  going in flight and landing, gas consumed, funds released with the tools down.
  These are the line items (`EXECUTION.md`, "Holdings and custody").
- **The work log**: every action, its steps, and each side effect with how it
  ended.
- **The header**: the operator (legal name, tax ID), times, the inputs and
  outputs asked for, the quote's estimates, and how the job ended and why.

It's served as JSON and as an HTML page.

## Planned

### Rates

Each item will carry the exchange rates at its time, into a reporting currency
the operator chooses (not assumed to be USD), with each rate's source. Midmarket
rates aren't representative and make phantom gains and losses, so the invoice
records exactly the rate and source used, and anyone can recompute with their
own from the raw amounts and times.

### Fees and tax

- The operator's fee: a configured percentage of the input's value, taken from
  the output side, with VAT (a configured rate) on top.
- The operator's payout goes to an address the route picks among ones the
  operator configured, to keep its transfer cheap. The gas for that transfer is
  the operator's expense, recorded on the payout.
- New item types: `swap` (with the rate achieved and slippage), `protocol_fee`,
  `fee`, `tax`, `operator_payout`.

### Accounting use

For the user, each item with a rate is a taxable event or a deductible expense
depending on where they are; the invoice gives the raw facts. For the operator,
fee items are revenue, tax items are tax payable, payout gas is an expense;
hosting and salaries are outside the invoice.

### Persistence

Invoices kept on the TEE's encrypted `/data`, which survives reboots but can't
be read if the code or configuration changes. This is part of durable job state
(`DESIGN.md`, "Direction").
