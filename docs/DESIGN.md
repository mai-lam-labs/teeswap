# TEESwap design

TEESwap moves a user's funds where they asked, from inside a TEE, so that the
operator of the machine can neither take the funds nor misreport what happened.
Every answer it gives is attested.

**Today** it does same-chain transfers on EVM chains, split across any number of
recipients: ETH funded by deposit, USDC funded by deposit or by an x402 payment,
or inputs in accounts whose keys the user hands over. **Next** are swaps and
bridges (`PROTOCOLS.md`).

The executor is personified as Mai. How she runs a job is in `EXECUTION.md`, and
how she takes payment in `X402.md`.

## Trust model

**The operator is an adversary.** They control the host, the network, the host's
clock, and the cloud metadata the configuration comes from. What's trusted is
what's measured: the code and the configuration, measured into TPM PCRs at boot
(`LOCKBOOT.md`). Everything else read at runtime (the configuration's contents,
RPC answers, facilitator answers, the time) is untrusted input. Configuration
can't change anything security-sensitive: keys, bind addresses, privileges and
binary paths are fixed in the code.

**Keys come from the TPM.** The signing key, the HPKE key for blind calls, and the
root key that every job's accounts are derived from, are derived by the TPM and
bound to the PCRs. The same code and configuration give the same keys on every
boot, so a job's accounts can be derived again after a restart. A change to
either gives different keys, so the accounts of jobs still running are out of
reach from then on: they have to be finished or handed over first.

**The invoice id is the password.** Whoever holds it owns the invoice, the funds
it holds, and their recovery. It goes nowhere but back to its owner: never into
logs, the operator's dashboard, URLs, error messages, or anything sent to a third
party. Anything that needs to name an invoice without granting access uses its
one-way `ref`.

**Every result is attested.** Each tool result carries a Verifiable MCP
`tee-vaportpm-v1` proof (`reference/VERIFIABLE_MCP.md`) binding the arguments and
the result to the enclave's measurements, so a result can't be forged or altered
on the way.

**Secrets travel only in blind calls.** A blind call's arguments are encrypted to
the TEE's HPKE key, and its reply to a key the client provides. Tools that take
or return keys are blind-only: never plain MCP or REST, where the operator can
read what passes.

**The chain is the evidence.** Whether something happened is read from chain
state (a receipt, an account's nonce, a token's `authorizationState`, a balance),
and time from the chain's blocks, never from the host's clock. Nothing is found
by searching history: no scan grows with the chain.

**Only the users' funds.** The operator never puts in money: every cost comes
from the job's own funds, within the reserve its quote set aside.

## Interfaces

Each tool is served over MCP (JSON-RPC: the stateful `2025-11-25` by default,
the stateless `2026-07-28` when asked for) and over REST, except the blind-only
tools. The paid tool speaks each transport's own x402. The tools are listed in
the README.

Types are defined once, and the MCP, REST and OpenAPI schemas are derived from
them, so the interfaces can't drift apart. The same holds for errors: each
domain error's class travels with it, and a client raises the same error
whichever way it called.

`Api` is the typed Python interface to the tools: in process, as a REST client,
or as an MCP client, all at parity. Every flow is tested once through each.

The operator's dashboard shows invoices by their `ref`, never their id.

## How it runs

A single process with a single event loop: every job, key and record lives in
that one process. Jobs run as tasks in it, each driven by the engine and planned
by the planner (`EXECUTION.md`). RPCs and facilitators come from the
configuration and are monitored; facilitators are ranked by how reliable they've
been, never ruled out for failing (`X402.md`).

## Decisions

The ones that shape everything else, with the reasons in the docs named:

- **No refunds; the tools go down.** When Mai can't finish, she hands the owner
  the keys to the job's accounts. It works even when she can't act, and needs no
  choice of where money goes (`EXECUTION.md`).
- **Handed-over keys can fund the next job.** Recovery and retrying are the same
  thing (`EXECUTION.md`).
- **A side effect is resolved by watching, never by acting again.** A failed send
  isn't a failure; only whoever enforces its idempotency key can say it didn't
  happen (`EXECUTION.md`).
- **Costs are budgeted.** Mai goes ahead only when the job can pay for finishing,
  and an overpayment is leave to spend it on finishing (`EXECUTION.md`).
- **An x402 payment's answer is final**, and a payment is checked inside the TEE
  before any facilitator sees it (`X402.md`).
- **A failure moves a facilitator down; it never removes it** (`X402.md`).

## Direction

- **Deadlines.** Each quote estimates how long the route takes when things go
  normally, and the user accepts it with the quote. By the deadline the job is
  over one way or the other: delivered, or the tools down.
- **Swaps and bridges**, routed for risk as well as price (`PROTOCOLS.md`).
- **Durable job state**, so a restarted TEE resumes its jobs from the record,
  which for Mai is just re-planning.
- **Fees and a full invoice**: rates, the operator's fee and tax (`INVOICING.md`).
- **The attestation proxy**: a small local binary that verifies the TEE's
  attestation, makes blind calls, and offers plain MCP to the agent. With it,
  `Api`'s MCP client should also check the TEE's HPKE key against the boot
  attestation.
- **The owner's channel.** Invoice pages are served at URLs that contain the id,
  which breaks the rule above. Either the TEE serves TLS itself, or calls that
  carry the id become blind-only.
- **Input provenance**: prices proven to come from where they say (`oracle-sig`,
  `zktls`).
