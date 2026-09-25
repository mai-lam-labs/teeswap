"""The planner: which operations reach a job's goal (see docs/EXECUTION.md).

One place decides routing, for both uses:

- provisional_plan(): the quote's dry run, from the goal alone, before any funds exist.
- decide(): for a running job, from what its holdings show now: either the next
  operations, or none, having finished the job (delivered, or tools down) and why.
  Called when the job starts and again each time the scheduled operations finish.

Today's routes are same-chain transfers on an EVM chain: the native token, sent by
Mai, or an EIP-3009 token, sent by a facilitator (which pays the gas). The inputs
arrive by deposit or, for a token, by an x402 payment.
"""

from collections.abc import Callable

from ..blockchain.chains import Chain, ChainFamily
from ..blockchain.rpc import JsonRpcError
from ..common import TeeSwapError
from ..types import Balance, Timestamp, Token, TokenAmount
from .accounts import Accounts
from .invoice import Funding, InputStatus, Invoice, InvoiceOutput, InvoiceStatus, OutputStatus
from .ledger import Custody, Place
from .operations import AwaitDeposit, EvmFor, FacilitatedTransfer, NativeTransfer, Operation

# the facilitators Mai may use on a chain, best first
type FacilitatorsFor = Callable[[Chain], tuple[str, ...]]


class RouteError(TeeSwapError):
    pass


class NoRouteError(RouteError):
    pass


def check_route(
    inputs: tuple[TokenAmount, ...],
    outputs: tuple[Balance, ...],
    funding: Funding,
    facilitators_for: FacilitatorsFor,
) -> None:
    if len(inputs) != 1:
        raise NoRouteError(f"expected exactly one input token, got {len(inputs)}")
    token = inputs[0].token
    if token.chain.family != ChainFamily.EVM:
        raise NoRouteError(f"only EVM transfers are supported, not {token.chain.name}")
    if token.contract is None:
        if funding == Funding.X402:
            raise NoRouteError(f"x402 pays in tokens, not native {token.symbol}")
    elif not facilitators_for(token.chain):
        raise NoRouteError(f"no facilitator available for {token.symbol} on {token.chain.name}")
    if not outputs:
        raise NoRouteError("no outputs")
    for out in outputs:
        if out.amount.token != token:
            raise NoRouteError(
                f"output {out.amount.token.symbol} differs from input {token.symbol}"
            )


def provisional_plan(
    inputs: tuple[TokenAmount, ...],
    outputs: tuple[Balance, ...],
    accounts: Accounts,
    funding: Funding,
    facilitators_for: FacilitatorsFor,
) -> list[Operation]:
    """What Mai expects to do for this goal, from nothing held yet."""
    check_route(inputs, outputs, funding, facilitators_for)
    deposits = tuple(
        Balance(amount=inp, address=accounts.input(i, inp.token.chain).address)
        for i, inp in enumerate(inputs)
    )
    operations: list[Operation] = [AwaitDeposit(d) for d in deposits]
    operations += [_transfer(out, _source(deposits, out), facilitators_for) for out in outputs]
    return operations


# attempts at an output that came back before it's given up on. Costs are what normally
# stop retrying (see _unaffordable); this stops attempts that cost nothing, like a
# transfer a node keeps refusing, from repeating forever
MAX_ATTEMPTS = 5


async def decide(
    invoice: Invoice, facilitators_for: FacilitatorsFor, evm_for: EvmFor
) -> tuple[Operation, ...]:
    """What happens next for a running job, from its holdings: the next operations, or
    none, having finished the invoice with how it ended and why.

    A job ends one of two ways: delivered, or tools down, where Mai stops and the job's
    result is the money itself, handed to the owner (see Invoice.TOOLS_DOWN). She puts
    them down when finishing is impossible, or would cost more than the job can spare.
    """
    inputs = invoice.input_states()
    outputs = invoice.output_states()
    if invoice.tools_down_requested:
        invoice.finish(InvoiceStatus.TOOLS_DOWN, "the owner put the tools down")
        return ()
    if all(out.status == OutputStatus.DELIVERED for out in outputs):
        invoice.finish(InvoiceStatus.DELIVERED, "every output was delivered")
        return ()
    awaited = [inp for inp in inputs if inp.status != InputStatus.RECEIVED]
    if awaited:
        if Timestamp.now() > invoice.expires_at:
            invoice.finish(InvoiceStatus.TOOLS_DOWN, "the inputs didn't arrive in time")
            return ()
        # nothing else until the funds are held: then what finishing costs can be weighed
        return tuple(AwaitDeposit(inp.deposit) for inp in awaited)
    for out in outputs:
        if out.status == OutputStatus.PENDING and out.attempts >= MAX_ATTEMPTS:
            invoice.finish(
                InvoiceStatus.TOOLS_DOWN,
                f"{out.attempts} transfers to {out.balance.address.value} came back",
            )
            return ()
    try:
        operations = [
            _transfer(out.balance, _source(invoice.deposits, out.balance), facilitators_for)
            for out in outputs
            if out.status == OutputStatus.PENDING
        ]
    except NoRouteError as e:
        invoice.finish(InvoiceStatus.TOOLS_DOWN, f"no way to finish: {e}")
        return ()
    try:
        costs = [cost for op in operations for cost in await op.estimate_costs(evm_for)]
    except JsonRpcError as e:
        # the chain answered: simulating the rest of the job fails
        invoice.finish(InvoiceStatus.TOOLS_DOWN, f"the rest can't be done: {e.rpc_message}")
        return ()
    shortfall = _unaffordable(invoice, outputs, costs)
    if shortfall is not None:
        invoice.finish(InvoiceStatus.TOOLS_DOWN, shortfall)
        return ()
    return tuple(operations)


def _unaffordable(
    invoice: Invoice, outputs: tuple[InvoiceOutput, ...], costs: list[TokenAmount]
) -> str | None:
    """Why the next plan can't be paid for, if it can't.

    What the job can spend on costs is what it holds beyond what it still owes the
    outputs: the quote's reserve, less what's been consumed, plus anything paid over the
    quote (an overpayment is taken as leave to spend it on finishing). The outputs
    themselves are never spent on costs.
    """
    totals: dict[Token, int] = {}
    for cost in costs:
        totals[cost.token] = totals.get(cost.token, 0) + cost.amount
    for token, needed in totals.items():
        held = sum(
            p.amount.amount
            for p in invoice.holdings.positions
            if p.place.custody == Custody.HELD and p.amount.token == token
        )
        owed = sum(
            out.balance.amount.amount
            for out in outputs
            if out.status == OutputStatus.PENDING and out.balance.amount.token == token
        )
        spare = max(held - owed, 0)
        if needed > spare:
            return f"finishing needs {needed} {token.symbol} for costs, and {spare} is spare"
    return None


def _source(deposits: tuple[Balance, ...], output: Balance) -> Place:
    """An output is paid from the account its token was deposited to."""
    for deposit in deposits:
        if deposit.amount.token == output.amount.token:
            return Place(address=deposit.address, custody=Custody.HELD)
    raise NoRouteError(f"no input of {output.amount.token.symbol} to pay the output from")


def _transfer(output: Balance, source: Place, facilitators_for: FacilitatorsFor) -> Operation:
    token = output.amount.token
    if token.contract is None:
        return NativeTransfer(output, source)
    candidates = facilitators_for(token.chain)
    if not candidates:
        raise NoRouteError(f"no facilitator available for {token.symbol} on {token.chain.name}")
    return FacilitatedTransfer(output, source, candidates)
