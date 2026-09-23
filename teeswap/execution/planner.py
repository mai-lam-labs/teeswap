"""The planner: which operations reach a job's goal (see docs/EXECUTION.md).

One place decides routing, for both uses:

- provisional_plan(): the quote's dry run, from the goal alone, before any funds exist.
- plan(): the next operations for a running job, from what its holdings show now.
  Called when the job starts and again whenever Mai re-evaluates.

Today's routes are same-chain transfers on an EVM chain: the native token, sent by
Mai, or an EIP-3009 token, sent by a facilitator (which pays the gas). The inputs
arrive by deposit or, for a token, by an x402 payment.
"""

from collections.abc import Callable

from eth_typing import ChecksumAddress

from ..blockchain.chains import Chain, ChainFamily
from ..common import TeeSwapError
from ..types import Address, Balance, TokenAmount
from .invoice import Funding, InputStatus, Invoice, OutputStatus
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
    job_address: ChecksumAddress,
    funding: Funding,
    facilitators_for: FacilitatorsFor,
) -> list[Operation]:
    """What Mai expects to do for this goal, from nothing held yet."""
    check_route(inputs, outputs, funding, facilitators_for)
    operations: list[Operation] = [
        AwaitDeposit(Balance(amount=inp, address=Address(inp.token.chain, job_address)))
        for inp in inputs
    ]
    operations += [
        _transfer(
            out,
            Place(address=Address(out.address.chain, job_address), custody=Custody.HELD),
            facilitators_for,
        )
        for out in outputs
    ]
    return operations


def plan(invoice: Invoice, facilitators_for: FacilitatorsFor) -> list[Operation]:
    """What Mai does next for a running job, from its holdings."""
    operations: list[Operation] = [
        AwaitDeposit(inp.deposit)
        for inp in invoice.input_states()
        if inp.status != InputStatus.RECEIVED
    ]
    operations += [
        _transfer(out.balance, invoice.held(out.balance.address.chain), facilitators_for)
        for out in invoice.output_states()
        if out.status == OutputStatus.PENDING
    ]
    return operations


def _transfer(output: Balance, source: Place, facilitators_for: FacilitatorsFor) -> Operation:
    token = output.amount.token
    if token.contract is None:
        return NativeTransfer(output, source)
    candidates = facilitators_for(token.chain)
    if not candidates:
        raise NoRouteError(f"no facilitator available for {token.symbol} on {token.chain.name}")
    return FacilitatedTransfer(output, source, candidates[0])
