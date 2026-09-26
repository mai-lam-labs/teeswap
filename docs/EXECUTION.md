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
- **Accounts**: the addresses the job controls, where funds are held while
  Mai works on them.

A job has no single address. Each account is a key on one chain, of that
chain's own key type, derived inside the TEE from the job's seed (itself
derived from the TEE's root key and the invoice id) for a purpose (each input gets its own deposit account). Two chains with different
key types give accounts with nothing in common, not even their format. An
action asks for the account it needs; asking again returns the same one. A
chain whose key type Mai can't make has no accounts, so it can't be routed.

## Holdings and custody

Each position in the holdings is an amount of a token at a location, with a
custody state that answers "who can move this right now?":

| Custody | Meaning |
|---|---|
| held | at one of the job's accounts; the job controls it |
| in flight | committed to something not yet final (a sent transaction, an order, a bridge deposit), with its reference so it can be checked and resumed |
| delivered | at a recipient; it has left custody |
| consumed | spent: gas, protocol fees |
| released | handed back with the tools down: the owner holds the account's key |

Funds only change position through **movements** reported by actions. The
movements are the invoice's line items (see `INVOICING.md`): the record of
custody and the invoice are the same thing.

## Actions

An action does one thing (await a deposit, transfer to a recipient) and
reports what actually happened as movements, from its own evidence: what
arrived at the address, a transaction receipt, a settlement result. Actions
own the truth about their effects; nothing else second-guesses them.

An action is made of **steps**, and a step may perform **side effects**: each
act of reaching the outside world (a transaction broadcast to a node, an
authorization handed to a facilitator, later an order posted or a file
uploaded). A side effect is recorded on its step before it is performed, with
what is needed to perform it again or check it. Funds it commits are in flight
against that step, and the ledger points at the step.

Side effects that repeat the same act share an **idempotency key** that the
outside world enforces: a transaction's account and nonce, an EIP-3009
authorization's nonce, an order's id. However many times it was sent, at most
one takes effect. Retrying means sending the same thing again under the same
key (the same signed bytes to another node, the same authorization to another
facilitator), never a new act while the old one could still land.

"Did it take effect?" is asked per key, of whoever enforces it, and only it can
answer: it **landed** (with the evidence), it is **void** (it never can: the
nonce went to another transaction, the authorization expired unused), or it is
still **pending**. A request that failed or timed out is not an answer: the
other side may have carried it out anyway. A refusal is one, when it covers
every copy: a transaction every node refused was never held by anyone but Mai,
so it is void. A step whose side effects haven't resolved is waiting; it is
resolved by watching, never by acting again.

Funds arriving unasked for (dust, a second deposit) are noticed by the next
action that reads that address, and anything left at the end by the final
action; they are recorded as movements like everything else.

## The engine

The engine runs whatever the planner scheduled and applies what it reports.
When the scheduled work is done, or a step of it fails, it asks the planner
again. It knows nothing about chains, tokens or gas, and it decides nothing
about the job: each action reports how its own steps ended, and the planner
decides what happens next, including that the job is finished: delivered, or
tools down (below).

The engine's own role is a guardrail for what is truly broken rather than
merely unsuccessful: an error escaping an action or the planner, books that
don't add up, an action that ends without reporting how, a planner with
nothing to do for an unfinished job, passes that make no progress. Then it puts
the tools down, and records why. What's only unavailable for now (a chain that
doesn't answer, every facilitator down) isn't broken: the engine waits and asks
the planner again.

The invoice and its work log are the record of all this, for introspection.
Actions report into them; nothing reads them back to decide what to do.

## Plans and re-evaluation

The planner turns holdings and goal into a plan. The **quote is the first
plan**, made from the expected state before any funds exist: Mai's estimate of
what she can deliver and how, honest about cost but not binding on execution.

Mai follows the plan until something says it no longer fits: an action fails,
an outcome is off-plan (a short fill, a gas spike, a price move), or re-planning
is asked for. Then she:

1. stops starting new actions;
2. resolves every waiting step, since a side effect can't be taken back and
   its outcome must land in the holdings first;
3. plans afresh from the holdings; the new plan replaces what remained.

The job is delivered when every output is, not merely when nothing is left to
plan.

**Recovery is the same thing.** After a restart, in-flight actions resume from
their records and report their movements, then Mai re-plans from the holdings.
There is no separate recovery path. This needs the job's state to be durable,
which is a separate piece of work; actions are written to be resumable either
way.

## Tools down

A job ends one of two ways. Either the outputs are delivered, or Mai puts the
tools down: she stops, and **the job's result is the money itself**. Every
account the job controls is handed to the invoice's owner, with its key and
what the chain shows it holding. The funds don't move; control of them does.

The tools go down when the planner finds no way to finish (no route, the
inputs didn't arrive in time, the rest of the job fails when simulated), when
finishing would cost more than the job can spare, when the engine's guardrails
trip, or when the owner asks.

**What the job can spare** is what it holds beyond what it still owes the
outputs: the quote's reserve for costs, less what has been spent, plus anything
paid over the quote. Before each plan, the planner has every operation estimate
its cost at today's prices, and only goes ahead if the job can pay for it. So
retrying goes on while it's affordable, a gas spike stops the job before it
overspends, and the outputs are never spent on costs. The quote's reserve is
priced at the fee a transaction commits to, not the typical fee.

Whatever was in flight is normally watched to its end first; when a guardrail
stops a broken action partway, it may not be, which is why the handover reads
balances from the chain rather than from the record. The invoice records why,
keeps anything already delivered, and marks what was held as released once the
owner has the keys.

This replaces refunds. A refund needs Mai to choose where the money goes and to
still be able to move it; handing over the keys needs neither. It works when
she can't act (no gas, no facilitator, a bug), and the keys can be the input to
whatever the owner asks for next, from Mai or anywhere else.

The owner is whoever holds the invoice id: it is the credential. The keys go
only to a blind call with an encrypted reply (Verifiable MCP), never over an
interface whose replies the operator could read.

## Keys as inputs

The other half of tools down. A job's inputs can be accounts the client
already holds the keys to, for example the accounts a job handed over: the
client passes the keys in (again blind-only), the accounts become the new
job's input accounts, and what each holds is its input. Nothing is deposited
and nothing moves until the new plan says so.

The client keeps the keys too. If they spend from an account while Mai works,
the job copes with what the chain then shows, as it does with any surprise;
it's the client's choice to make.

## Arrivals

Mai is generous about what arrives. If the goal is still reachable, she
carries on: 1.001 ETH against a 1 ETH quote runs the plan as quoted, and the
surplus stays as a visible held position. An overpayment is taken as leave to
spend it on finishing, much as the slippage tolerance is: if costs rise, the
surplus pays for them before the tools go down. Whatever is left, the owner
can take with the tools down. Anything the record missed is counted when the
tools go down: the handover reads the chain, and funds beyond what the record
accounts for are recorded as arrivals before they are released.

## Capabilities

| Input | Output | How |
|---|---|---|
| ETH, by deposit | ETH, same chain | Mai sends transfers from the input's account, paying gas from the held ETH; gas is estimated by simulating each transfer |
| USDC, by x402 or deposit | USDC, same chain | an x402 payment is settled into the input's account by a facilitator; for outputs Mai signs EIP-3009 authorizations from that account and a facilitator settles them, paying gas |

The USDC path never needs ETH in the job's accounts. Facilitators are free
(x402 defines no facilitator fees), so a facilitated transfer costs the job
nothing. One that fails a job's transfer ranks lower for that job, and Mai
re-plans with the next best (see X402.md, "Selection"). When the outcome of a
facilitated transfer is unclear, the token contract is the evidence: it records
whether the authorization was used.

## Not yet

- **Fees.** Mai runs as a pure execution engine for now; fee-taking is a
  separate part.
- **Durable job state.** Needed for recovery across restarts.
- **Deadlines.** Each quote will estimate how long its route takes when things
  go normally, for the user to accept with it. By then the job is over, one way
  or the other; a delay that leaves no hope of finishing in time puts the tools
  down early, rather than waiting on a counter.
