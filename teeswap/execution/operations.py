"""Operations: the things Mai does for a job (see docs/EXECUTION.md).

Each operation does one thing and reports what actually happened as Movements,
from its own evidence (balances it observed, the chain's answer about its side
effects). It reads the job, but changes it only by reporting: the engine applies
movements to the holdings.

An operation that reaches the outside world does so through side effects
(effects.py), recorded on its work-log step before they are performed, with the
funds they commit reported in flight against that step. Its run() performs
them, then watches until whoever enforces their key says whether they took
effect, reports where the funds ended up, and reports how its step ended. It
doesn't return before that: a question the chain didn't answer is asked again,
never taken as an answer.

Every operation can also estimate its own costs, which is how the quote prices
Mai's provisional plan without running it.
"""

import abc
import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import override

import httpx
from eth_typing import HexStr
from eth_utils.address import to_checksum_address

from .. import facilitator
from ..blockchain.chains import Chain
from ..blockchain.evm import EncodedCall, EthSigner, EvmChain
from ..blockchain.rpc import JsonRpcError
from ..common import TeeSwapError
from ..http import BaseHttpClient
from ..types import Amount, Balance, Hex32, Timestamp, Token, TokenAmount, TxHash, Url
from ..x402 import (
    PaymentError,
    PaymentPayload,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
    SignedPayment,
    sign_exact_evm,
)
from .accounts import Accounts
from .effects import (
    AuthorizationSubmission,
    Landed,
    Outcome,
    Pending,
    SideEffect,
    TransactionBroadcast,
    Void,
)
from .invoice import Invoice, InvoiceStatus
from .ledger import Custody, Movement, MovementKind, Place
from .worklog import StepReport

logger = logging.getLogger(__name__)

DEPOSIT_POLL_INTERVAL = 5.0
TRANSACTION_POLL_INTERVAL = 0.5
REBROADCAST_EVERY = 60  # polls
AUTHORIZATION_POLL_INTERVAL = 1.0
# how far a payer's clock-based validity may run past what was asked, for clock skew
CLOCK_SLACK_SECONDS = 60

type EvmFor = Callable[[Chain], EvmChain]


class OperationError(TeeSwapError):
    pass


@dataclass(frozen=True, slots=True)
class OperationContext:
    """What an operation may use: the job (read-only), its accounts, chain access, and report."""

    invoice: Invoice
    accounts: Accounts
    step: StepReport  # the operation reports its own step: side effects, outcome, how it ended
    client: BaseHttpClient  # records RPC traffic on the operation's work-log step
    rpc_url_for: Callable[[Chain], Url]
    report: Callable[[Movement], None]
    # a facilitator that failed this job: the planner won't choose it again
    exclude_facilitator: Callable[[str], None]

    def evm(self, chain: Chain) -> EvmChain:
        return EvmChain(chain, self.client, self.rpc_url_for(chain))


class Operation(abc.ABC):
    @property
    @abc.abstractmethod
    def description(self) -> str: ...

    @property
    @abc.abstractmethod
    def chain(self) -> Chain: ...

    @property
    @abc.abstractmethod
    def phase(self) -> InvoiceStatus:
        """The job's status while this operation runs."""

    @abc.abstractmethod
    async def estimate_costs(self, evm_for: EvmFor) -> tuple[TokenAmount, ...]:
        """What running this is expected to consume, without running it."""

    @abc.abstractmethod
    async def run(self, ctx: OperationContext) -> None:
        """Do it, and report how the step ended (completed, or failed with a reason).
        Returns only once every side effect it performed has resolved."""


class AwaitDeposit(Operation):
    """Wait until the input has arrived at the job's address; report each arrival."""

    def __init__(self, deposit: Balance) -> None:
        self._deposit = deposit

    @property
    @override
    def description(self) -> str:
        amount = self._deposit.amount
        return f"wait for {amount.amount} {amount.token.symbol} at {self._deposit.address.value}"

    @property
    @override
    def chain(self) -> Chain:
        return self._deposit.address.chain

    @property
    @override
    def phase(self) -> InvoiceStatus:
        return InvoiceStatus.AWAITING_DEPOSIT

    @override
    async def estimate_costs(self, evm_for: EvmFor) -> tuple[TokenAmount, ...]:
        return ()

    @override
    async def run(self, ctx: OperationContext) -> None:
        token = self._deposit.amount.token
        address = self._deposit.address
        held = Place(address=address, custody=Custody.HELD)
        evm = ctx.evm(self.chain)
        while True:
            # arrivals are whatever the chain shows beyond what's already recorded as held
            try:
                observed = await evm.token_balance(token, to_checksum_address(address.value))
            except _UNANSWERED as e:
                logger.warning("%s: balance unavailable, retrying: %s", ctx.step.id, e)
                await asyncio.sleep(DEPOSIT_POLL_INTERVAL)
                continue
            arrived = observed - ctx.invoice.holdings.amount_at(held, token).amount.amount
            if arrived > 0:
                ctx.report(
                    Movement(
                        kind=MovementKind.INPUT,
                        amount=TokenAmount(token=token, amount=Amount(arrived)),
                        source=None,
                        destination=held,
                        timestamp=Timestamp.now(),
                    )
                )
            received = ctx.invoice.holdings.received(token, address)
            if received.amount.amount >= self._deposit.amount.amount:
                ctx.step.completed()
                return
            if Timestamp.now() > ctx.invoice.expires_at:
                ctx.step.failed("expired awaiting deposit")
                return
            if ctx.invoice.tools_down_requested:
                ctx.step.failed("the owner put the tools down")  # nothing in flight: stop now
                return
            await asyncio.sleep(DEPOSIT_POLL_INTERVAL)


class NativeTransfer(Operation):
    """Send an output in the chain's native token from where it's held, and see it land."""

    def __init__(self, output: Balance, source: Place) -> None:
        self._output = output
        self._source = source

    @property
    @override
    def description(self) -> str:
        amount = self._output.amount
        return f"send {amount.amount} {amount.token.symbol} to {self._output.address.value}"

    @property
    @override
    def chain(self) -> Chain:
        return self._output.address.chain

    @property
    @override
    def phase(self) -> InvoiceStatus:
        return InvoiceStatus.EXECUTING

    def _call(self) -> EncodedCall:
        return EncodedCall(
            to=to_checksum_address(self._output.address.value),
            data=HexStr("0x"),
            value=self._output.amount.amount,
        )

    @override
    async def estimate_costs(self, evm_for: EvmFor) -> tuple[TokenAmount, ...]:
        evm = evm_for(self.chain)
        source = to_checksum_address(self._source.address.value)
        units = await evm.estimate_gas_as_funded(source, self._call())
        gas = units * await evm.gas_price()
        return (TokenAmount(token=Token.native(self.chain), amount=Amount(gas)),)

    @override
    async def run(self, ctx: OperationContext) -> None:
        evm = ctx.evm(self.chain)
        try:
            signer = ctx.accounts.key(self._source.address, EthSigner)
            tx = await evm.prepare(signer, self._call())
        except _UNANSWERED as e:
            ctx.step.failed(f"could not prepare the transfer: {e}")  # nothing was sent
            return
        broadcast = TransactionBroadcast(self.chain, tx, ctx.rpc_url_for(self.chain))
        ctx.step.record(broadcast)
        in_flight = _in_flight(self._output, ctx.step)
        amount = self._output.amount
        ctx.report(_movement(MovementKind.TRANSIT, amount, self._source, in_flight))
        await _broadcast(evm, broadcast)
        ctx.step.waiting()

        polls = 0
        while isinstance(outcome := await _check(broadcast, ctx), Pending):
            polls += 1
            if polls % REBROADCAST_EVERY == 0:
                # the node may have dropped it: send the same bytes again, never a new transaction
                broadcast = TransactionBroadcast(self.chain, tx, ctx.rpc_url_for(self.chain))
                ctx.step.record(broadcast)
                await _broadcast(evm, broadcast)
            await asyncio.sleep(TRANSACTION_POLL_INTERVAL)
        ctx.step.resolved(broadcast.key, outcome)

        if isinstance(outcome, Void):
            ctx.report(_movement(MovementKind.TRANSIT, amount, in_flight, self._source))
            ctx.step.failed(f"transfer not sent: {outcome.reason}")
            return
        if outcome.gas is not None:
            consumed = Place(address=self._source.address, custody=Custody.CONSUMED)
            ctx.report(
                _movement(
                    MovementKind.GAS, outcome.gas, self._source, consumed, outcome.transaction
                )
            )
        if not outcome.success:
            # the value never left: back to where it was held
            ctx.report(
                _movement(
                    MovementKind.TRANSIT, amount, in_flight, self._source, outcome.transaction
                )
            )
            ctx.step.failed(f"transfer {outcome.transaction} reverted")
            return
        delivered = Place(address=self._output.address, custody=Custody.DELIVERED)
        ctx.report(
            _movement(MovementKind.OUTPUT, amount, in_flight, delivered, outcome.transaction)
        )
        ctx.step.completed()


# how long Mai's own authorizations stay usable: the longest an unanswered one is watched
AUTHORIZATION_TTL_SECONDS = 60


class FacilitatedTransfer(Operation):
    """Send a token output by EIP-3009 authorization, settled (and paid for) by a facilitator.

    Mai signs one transferWithAuthorization from the source account to the recipient
    and hands it to the facilitators in turn until one accepts it. They all hold the
    same authorization, so at most one transfer can happen; the token contract says
    which, if any.
    """

    def __init__(self, output: Balance, source: Place, facilitators: tuple[str, ...]) -> None:
        if output.amount.token.contract is None:
            raise OperationError(f"{output.amount.token.symbol} is native: no EIP-3009")
        if not facilitators:
            raise OperationError("no facilitator to settle the transfer")
        self._output = output
        self._source = source
        self._facilitators = facilitators

    @property
    @override
    def description(self) -> str:
        amount = self._output.amount
        return (
            f"send {amount.amount} {amount.token.symbol} to {self._output.address.value}"
            " via an x402 facilitator"
        )

    @property
    @override
    def chain(self) -> Chain:
        return self._output.address.chain

    @property
    @override
    def phase(self) -> InvoiceStatus:
        return InvoiceStatus.EXECUTING

    @override
    async def estimate_costs(self, evm_for: EvmFor) -> tuple[TokenAmount, ...]:
        return ()  # the facilitator pays the gas, and facilitators are free

    @override
    async def run(self, ctx: OperationContext) -> None:
        amount = self._output.amount
        token = amount.token
        contract = to_checksum_address(str(token.contract))
        evm = ctx.evm(self.chain)
        signer = ctx.accounts.key(self._source.address, EthSigner)
        try:
            name, version = await evm.eip712_domain(token)
            block = await evm.latest_block()
        except _UNANSWERED as e:
            ctx.step.failed(f"could not prepare the authorization: {e}")  # nothing was sent
            return

        requirements = PaymentRequirements(
            scheme="exact",
            network=self.chain.caip2,
            amount=amount.amount,
            asset=contract,
            payTo=to_checksum_address(self._output.address.value),
            maxTimeoutSeconds=AUTHORIZATION_TTL_SECONDS,
            extra={"name": name, "version": version},
        )
        resource = ResourceInfo(
            url=f"teeswap:invoice/{ctx.invoice.id.ref}",
            description=self.description,
            mimeType="application/json",
        )
        signed = sign_exact_evm(
            signer,
            resource,
            requirements,
            valid_after=block.timestamp - 60,
            valid_before=block.timestamp + AUTHORIZATION_TTL_SECONDS,
            nonce=Hex32.from_bytes(os.urandom(32)),
        )

        in_flight = _in_flight(self._output, ctx.step)

        def commit() -> None:
            # committed from the first submission on: any of them may settle it
            ctx.report(_movement(MovementKind.TRANSIT, amount, self._source, in_flight))

        submitted = await _submit_in_turn(
            ctx, token, signed, requirements, self._facilitators, commit
        )
        latest = submitted.submission
        if submitted.settle_attempted:
            ctx.step.waiting()
            while isinstance(outcome := await _check(latest, ctx), Pending):
                await asyncio.sleep(AUTHORIZATION_POLL_INTERVAL)
        else:
            # only facilitators hold the authorization, and none was asked to settle it
            outcome = Void(f"no facilitator accepted it: {submitted.why}")
        ctx.step.resolved(latest.key, outcome)

        if isinstance(outcome, Void):
            ctx.report(_movement(MovementKind.TRANSIT, amount, in_flight, self._source))
            ctx.step.failed(f"transfer not settled: {outcome.reason}")
            return
        delivered = Place(address=self._output.address, custody=Custody.DELIVERED)
        ctx.report(
            _movement(MovementKind.OUTPUT, amount, in_flight, delivered, outcome.transaction)
        )
        ctx.step.completed()


class ReceivePayment(Operation):
    """Receive an input by x402 payment: hand the payer's signed authorization to the
    facilitators in turn, then watch until the funds are in the input's account or the
    authorization can no longer be used. Nothing is reported as failed that could still
    succeed, so a payer is never asked to pay twice for the same thing.

    The funds arriving is what counts: if they're there, the payment succeeded, whether
    or not a facilitator said so.
    """

    def __init__(
        self,
        deposit: Balance,
        payment: PaymentPayload,
        requirements: PaymentRequirements,
        facilitators: tuple[str, ...],
    ) -> None:
        self._deposit = deposit
        self._payment = payment
        self._requirements = requirements
        self._facilitators = facilitators
        self.settlement: SettleResponse | None = None  # once received: how it settled

    @property
    @override
    def description(self) -> str:
        amount = self._deposit.amount
        return (
            f"receive {amount.amount} {amount.token.symbol} at {self._deposit.address.value}"
            " by x402 payment"
        )

    @property
    @override
    def chain(self) -> Chain:
        return self._deposit.address.chain

    @property
    @override
    def phase(self) -> InvoiceStatus:
        return InvoiceStatus.QUOTED  # not accepted until it's paid

    @override
    async def estimate_costs(self, evm_for: EvmFor) -> tuple[TokenAmount, ...]:
        return ()  # the facilitator pays the gas

    @override
    async def run(self, ctx: OperationContext) -> None:
        token = self._deposit.amount.token
        evm = ctx.evm(self.chain)
        try:
            signed = SignedPayment.from_payload(self._payment)
            block = await evm.latest_block()
        except PaymentError as e:
            ctx.step.failed(str(e))
            return
        except _UNANSWERED as e:
            ctx.step.failed(f"could not check the payment: {e}")  # nothing was sent
            return
        # it's watched until it lands or expires: that must not be longer than was asked
        latest_valid = block.timestamp + self._requirements.maxTimeoutSeconds + CLOCK_SLACK_SECONDS
        if signed.authorization.valid_before > latest_valid:
            ctx.step.failed("the payment stays valid for longer than maxTimeoutSeconds")
            return
        if not self._facilitators:
            ctx.step.failed("no facilitator available")
            return

        submitted = await _submit_in_turn(
            ctx, token, signed, self._requirements, self._facilitators, lambda: None
        )
        latest = submitted.submission
        if not submitted.settle_attempted:
            reason = f"no facilitator accepted the payment: {submitted.why}"
            ctx.step.resolved(latest.key, Void(reason))
            ctx.step.failed(reason)
            return
        ctx.step.waiting()

        address = self._deposit.address
        held = Place(address=address, custody=Custody.HELD)
        while True:
            try:
                observed = await evm.token_balance(token, to_checksum_address(address.value))
            except _UNANSWERED as e:
                logger.warning("%s: balance unavailable, retrying: %s", ctx.step.id, e)
                observed = None
            if observed is not None:
                arrived = observed - ctx.invoice.holdings.amount_at(held, token).amount.amount
                if arrived >= self._deposit.amount.amount:
                    self._received(ctx, signed, latest, arrived)
                    return
            outcome = await _check(latest, ctx)
            if isinstance(outcome, Void):
                ctx.step.resolved(latest.key, outcome)
                ctx.step.failed(f"payment not settled: {outcome.reason}")
                return
            if isinstance(outcome, Landed) and observed is not None:
                # used, yet the funds aren't here: we don't have them, and never will
                reason = "the payment was used, but the funds didn't arrive"
                ctx.step.resolved(latest.key, Void(reason))
                ctx.step.failed(reason)
                return
            await asyncio.sleep(AUTHORIZATION_POLL_INTERVAL)

    def _received(
        self,
        ctx: OperationContext,
        signed: SignedPayment,
        latest: AuthorizationSubmission,
        arrived: int,
    ) -> None:
        token = self._deposit.amount.token
        ctx.step.resolved(latest.key, Landed(transaction=latest.settled))
        ctx.report(
            Movement(
                kind=MovementKind.INPUT,
                amount=TokenAmount(token=token, amount=Amount(arrived)),
                source=None,
                destination=Place(address=self._deposit.address, custody=Custody.HELD),
                timestamp=Timestamp.now(),
                transaction=latest.settled,
            )
        )
        # we have the funds: that is success, even if no facilitator said which
        # transaction carried them
        self.settlement = SettleResponse(
            success=True,
            payer=signed.authorization.sender,
            transaction=latest.settled or "",
            network=self.chain.caip2,
        )
        ctx.step.completed()


@dataclass(frozen=True, slots=True)
class _Submitted:
    """How handing an authorization to the facilitators went."""

    submission: AuthorizationSubmission  # the latest: all of them share one key
    settle_attempted: bool  # a facilitator was asked to settle it, so it may land
    why: str  # what the facilitators said, when none settled it


async def _submit_in_turn(
    ctx: OperationContext,
    token: Token,
    signed: SignedPayment,
    requirements: PaymentRequirements,
    facilitators: tuple[str, ...],
    before_first: Callable[[], None],
) -> _Submitted:
    """Hand the same signed authorization to each facilitator until one settles it, or it
    turns out to have settled anyway. A facilitator that errors or fails to settle is
    excluded from the job; one that finds the payment invalid is not (it's the payment)."""
    settle_attempted = False
    said: list[str] = []
    submission: AuthorizationSubmission | None = None
    for i, url in enumerate(facilitators):
        submission = AuthorizationSubmission(
            token.chain, token, signed.authorization, signed.signature, url
        )
        ctx.step.record(submission)
        if i == 0:
            before_first()
        submission.sending()
        try:
            verified = await facilitator.verify(ctx.client, url, signed.payload, requirements)
            if not verified.isValid:
                submission.failed(f"invalid: {verified.invalidReason}")
                said.append(f"{url}: invalid: {verified.invalidReason}")
                continue
            settle_attempted = True
            settled = await facilitator.settle(ctx.client, url, signed.payload, requirements)
        except facilitator.FacilitatorError as e:
            submission.failed(str(e))
            said.append(str(e))
            ctx.exclude_facilitator(url)
        else:
            if settled.success:
                submission.settled_by(TxHash(settled.transaction))
                return _Submitted(submission, settle_attempted=True, why="")
            reason = settled.errorReason or settled.errorMessage
            submission.failed(f"not settled: {reason}")
            said.append(f"{url}: not settled: {reason}")
            ctx.exclude_facilitator(url)
        # the facilitator may have settled it anyway: don't offer it again if so
        if settle_attempted and not isinstance(await _check(submission, ctx), Pending):
            break
    if submission is None:
        raise OperationError("no facilitator to submit to")
    return _Submitted(submission, settle_attempted=settle_attempted, why="; ".join(said))


# a request that errored or timed out: no answer, so never taken as one
_UNANSWERED = (JsonRpcError, httpx.HTTPError)


async def _check(effect: SideEffect, ctx: OperationContext) -> Outcome:
    """Ask whether the side effect took effect. Not getting an answer is not an answer."""
    try:
        return await effect.check(ctx.client, ctx.rpc_url_for)
    except _UNANSWERED as e:
        logger.warning("%s: outcome unavailable, still pending: %s", ctx.step.id, e)
        return Pending()


async def _broadcast(evm: EvmChain, broadcast: TransactionBroadcast) -> None:
    broadcast.sending()
    try:
        await evm.submit(broadcast.tx)
    except _UNANSWERED as e:
        # not proof it wasn't received: the chain is asked
        broadcast.failed(str(e))


def _in_flight(output: Balance, step: StepReport) -> Place:
    return Place(address=output.address, custody=Custody.IN_FLIGHT, step=step.id)


def _movement(
    kind: MovementKind,
    amount: TokenAmount,
    source: Place,
    destination: Place,
    transaction: TxHash | None = None,
) -> Movement:
    return Movement(
        kind=kind,
        amount=amount,
        source=source,
        destination=destination,
        timestamp=Timestamp.now(),
        transaction=transaction,
    )
