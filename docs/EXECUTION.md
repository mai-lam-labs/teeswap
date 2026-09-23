# Execution

How TEESwap carries out a job. The executor is personified as **Mai**: she gives
a quote, works to a plan, and when reality diverges she stops and re-plans from
wherever things actually are. It is business process management, not a script.

## The job

A job (the invoice) has:

- **Goal**: the inputs the user provides and the outputs they want delivered.
- **Holdings**: where every unit of the user's funds is right now, and who
  controls it. This is the job's core state.
- **Plan**: the actions Mai still intends to take.
- **In-flight actions**: started and not yet finished.
- **Work log**: the record of every action and what it moved.

Each job has its own key material (a signer derived inside the TEE). The
addresses it controls are where funds are held while Mai works on them.

## Holdings and custody

Each position in the holdings is an amount of a token at a location, with a
custody state that answers "who can move this right now?":

| Custody | Meaning |
|---|---|
| held | at the job's own address; the job's key controls it |
| in flight | committed to something not yet final (a sent transaction, an order, a bridge deposit), with its reference so it can be checked and resumed |
| delivered | at a recipient; it has left custody |
| consumed | spent: gas, protocol fees |

Funds only change position through **movements** reported by actions. The
movements are the invoice's line items (see `INVOICING.md`): the record of
custody and the invoice are the same thing.

## Actions

An action does one thing (await a deposit, transfer to a recipient) and
reports what actually happened as movements, from its own evidence: what
arrived at the address, a transaction receipt, a settlement result. Actions
own the truth about their effects; nothing else second-guesses them.

Before any side effect an action records its intent (e.g. the transaction it is
about to send). An action interrupted mid-flight can then resume by checking
its recorded reference instead of acting again. No side effect is ever
repeated blindly.

Funds arriving unasked for (dust, a second deposit) are noticed by the next
action that reads that address, and anything left at the end by the final
action; they are recorded as movements like everything else.

## The engine

The engine is only the process machine. It starts planned actions that are
ready (possibly several at once), applies the movements each one reports, and
handles re-evaluation. It knows nothing about chains, tokens or gas; actions
and the planner do.

## Plans and re-evaluation

The planner turns holdings and goal into a plan. The **quote is the first
plan**, made from the expected state before any funds exist: Mai's estimate of
what she can deliver and how, honest about cost but not binding on execution.

Mai follows the plan until something says it no longer fits: an action fails,
an outcome is off-plan (a short fill, a gas spike, a price move), or re-planning
is asked for. Then she:

1. stops starting new actions;
2. lets in-flight actions finish, since a sent transaction can't be unsent and
   its outcome must land in the holdings first;
3. plans afresh from the holdings; the new plan replaces what remained.

**Recovery is the same thing.** After a restart, in-flight actions resume from
their records and report their movements, then Mai re-plans from the holdings.
There is no separate recovery path. This needs the job's state to be durable,
which is a separate piece of work; actions are written to be resumable either
way.

## Arrivals

Mai is generous about what arrives. If the goal is still reachable, she
carries on: 1.001 ETH against a 1 ETH quote runs the plan as quoted, and the
surplus stays as a visible held position. What happens to surplus is decided
together with fees and refunds.

## Capabilities

| Input | Output | How |
|---|---|---|
| ETH, by deposit | ETH, same chain | Mai sends transfers from the job's address, paying gas from the held ETH; gas is estimated by simulating each transfer |
| USDC, by x402 or deposit | USDC, same chain | an x402 payment is settled into the job's address by a facilitator; for outputs Mai signs EIP-3009 authorizations from that address and a facilitator settles them, paying gas |

The USDC path never needs ETH at the job's address. Facilitators are free
(x402 defines no facilitator fees), so a facilitated transfer costs the job
nothing. One that fails a job's transfer is excluded from that job, and Mai
re-plans with the next (see X402.md, "Selection"). When the outcome of a
facilitated transfer is unclear, the token contract is the evidence: it records
whether the authorization was used.

## Not yet

- **Fees.** Mai runs as a pure execution engine for now; fee-taking is a
  separate part.
- **Cancellation and refunds.** Deferred: the nuanced part is deciding what
  can be unwound once funds are in flight.
- **Durable job state.** Needed for recovery across restarts.
