"""The planner: which operations reach a job's goal (see docs/EXECUTION.md).

One place decides routing, for both uses:

- provisional_plan(): the quote's dry run, from the goal alone, before any funds exist.
- decide(): for a running job, from what its holdings show now: either the next
  operations, or that the job is finished and why (delivered, expired, failed).
  Called when the job starts and again each time the scheduled operations finish.

Today's routes are same-chain transfers on an EVM chain: the native token, sent by
Mai, or an EIP-3009 token, sent by a facilitator (which pays the gas). The inputs
arrive by deposit or, for a token, by an x402 payment.
"""

from collections.abc import Callable
from dataclasses import dataclass

from ..blockchain.chains import Chain, ChainFamily
from ..common import TeeSwapError
from ..types import Balance, Timestamp, TokenAmount
from .accounts import Accounts
from .invoice import Funding, InputStatus, Invoice, InvoiceStatus, OutputStatus
from .ledger import Custody, Place
from .operations import AwaitDeposit, FacilitatedTransfer, NativeTransfer, Operation

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


# transfers that came back (void or reverted) before an output is given up on
MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Plan:
    operations: tuple[Operation, ...]


@dataclass(frozen=True, slots=True)
class Finished:
    status: InvoiceStatus  # DELIVERED or TOOLS_DOWN
    reason: str


type Decision = Plan | Finished


def decide(invoice: Invoice, facilitators_for: FacilitatorsFor) -> Decision:
    """What happens next for a running job, from its holdings: more work, or the end.

    A job ends one of two ways: delivered, or tools down, where Mai stops and the job's
    result is the money itself, handed to the owner (see Invoice.TOOLS_DOWN).
    """
    inputs = invoice.input_states()
    outputs = invoice.output_states()
    if invoice.tools_down_requested:
        return Finished(InvoiceStatus.TOOLS_DOWN, "the owner put the tools down")
    if all(out.status == OutputStatus.DELIVERED for out in outputs):
        return Finished(InvoiceStatus.DELIVERED, "every output was delivered")
    awaited = [inp for inp in inputs if inp.status != InputStatus.RECEIVED]
    if awaited and Timestamp.now() > invoice.expires_at:
        return Finished(InvoiceStatus.TOOLS_DOWN, "the inputs didn't arrive in time")
    for out in outputs:
        if out.status == OutputStatus.PENDING and out.attempts >= MAX_ATTEMPTS:
            return Finished(
                InvoiceStatus.TOOLS_DOWN,
                f"{out.attempts} transfers to {out.balance.address.value} came back",
            )
    try:
        operations: list[Operation] = [AwaitDeposit(inp.deposit) for inp in awaited]
        operations += [
            _transfer(out.balance, _source(invoice.deposits, out.balance), facilitators_for)
            for out in outputs
            if out.status == OutputStatus.PENDING
        ]
    except NoRouteError as e:
        return Finished(InvoiceStatus.TOOLS_DOWN, f"no way to finish: {e}")
    return Plan(tuple(operations))


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
