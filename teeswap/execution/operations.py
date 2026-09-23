"""Operations: the things Mai does for a job (see docs/EXECUTION.md).

Each operation does one thing and reports what actually happened as Movements,
from its own evidence (balances it observed, receipts). It reads the job, but
changes it only by reporting: the engine applies movements to the holdings.

An operation records its intent before any side effect: NativeTransfer reports the
in-flight movement, with its transaction hash, before submitting. Interrupted, it
can be resumed by checking that hash instead of sending again.

Every operation can also estimate its own costs, which is how the quote prices
Mai's provisional plan without running it.

Operations are named apart from the work-log Action/Step records: operations are
what runs, Action/Step are the record of what ran.
"""

import abc
import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from eth_typing import ChecksumAddress, HexStr
from eth_utils.address import to_checksum_address

from .. import facilitator
from ..blockchain.chains import Chain
from ..blockchain.evm import EncodedCall, EthSigner, EvmChain, TransferAuthorization
from ..common import TeeSwapError
from ..http import BaseHttpClient
from ..types import Amount, Balance, Hex32, Timestamp, Token, TokenAmount, TxHash, Url
from ..x402 import X402_VERSION, PaymentPayload, PaymentRequirements, ResourceInfo
from .invoice import Invoice, InvoiceExpiredError, InvoiceStatus, Step
from .ledger import Custody, Movement, MovementKind, Place

DEPOSIT_POLL_INTERVAL = 5.0

type EvmFor = Callable[[Chain], EvmChain]


class OperationError(TeeSwapError):
    pass


@dataclass(frozen=True, slots=True)
class OperationContext:
    """What an operation may use: the job (read-only), its key, chain access, and report."""

    invoice: Invoice
    signer: EthSigner
    step: Step
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
    async def estimate_costs(
        self, evm_for: EvmFor, job_address: ChecksumAddress
    ) -> tuple[TokenAmount, ...]:
        """What running this is expected to consume, without running it."""

    @abc.abstractmethod
    async def run(self, ctx: OperationContext) -> None: ...


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
    async def estimate_costs(
        self, evm_for: EvmFor, job_address: ChecksumAddress
    ) -> tuple[TokenAmount, ...]:
        return ()

    @override
    async def run(self, ctx: OperationContext) -> None:
        token = self._deposit.amount.token
        address = self._deposit.address
        held = Place(address=address, custody=Custody.HELD)
        evm = ctx.evm(self.chain)
        while True:
            # arrivals are whatever the chain shows beyond what's already recorded as held
            observed = await evm.token_balance(token, ctx.signer.address)
            arrived = observed - ctx.invoice.holdings.amount_at(held, token)
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
            if ctx.invoice.holdings.received(token, address) >= self._deposit.amount.amount:
                return
            if ctx.invoice.is_expired:
                raise InvoiceExpiredError(f"invoice {ctx.invoice.id} expired awaiting deposit")
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
    async def estimate_costs(
        self, evm_for: EvmFor, job_address: ChecksumAddress
    ) -> tuple[TokenAmount, ...]:
        evm = evm_for(self.chain)
        units = await evm.estimate_gas_as_funded(job_address, self._call())
        gas = units * await evm.gas_price()
        return (TokenAmount(token=Token.native(self.chain), amount=Amount(gas)),)

    @override
    async def run(self, ctx: OperationContext) -> None:
        amount = self._output.amount
        evm = ctx.evm(self.chain)
        tx = await evm.prepare(ctx.signer, self._call())
        ref = TxHash.from_bytes(tx.tx_hash)
        in_flight = Place(address=self._output.address, custody=Custody.IN_FLIGHT, reference=ref)

        # intent first: once this is recorded, the funds are accounted for by the tx hash
        ctx.report(_movement(MovementKind.TRANSIT, amount, self._source, in_flight, ref))
        await evm.submit(tx)
        receipt = await evm.wait_receipt(tx.tx_hash)

        gas = TokenAmount(token=Token.native(self.chain), amount=Amount(receipt.gas_cost))
        consumed = Place(address=self._source.address, custody=Custody.CONSUMED)
        ctx.report(_movement(MovementKind.GAS, gas, self._source, consumed, ref))
        if not receipt.success:
            # the value never left: back to where it was held
            ctx.report(_movement(MovementKind.TRANSIT, amount, in_flight, self._source, ref))
            raise OperationError(f"transfer {ref} reverted")
        delivered = Place(address=self._output.address, custody=Custody.DELIVERED, reference=ref)
        ctx.report(_movement(MovementKind.OUTPUT, amount, in_flight, delivered, ref))


AUTHORIZATION_TTL_SECONDS = 300


class FacilitatedTransfer(Operation):
    """Send a token output by EIP-3009 authorization, settled (and paid for) by a facilitator.

    Mai signs transferWithAuthorization from the job's address to the recipient; the
    facilitator verifies and settles it. The in-flight reference is the authorization's
    nonce, which the token contract can be asked about if the outcome is unclear.
    """

    def __init__(self, output: Balance, source: Place, facilitator: str) -> None:
        if output.amount.token.contract is None:
            raise OperationError(f"{output.amount.token.symbol} is native: no EIP-3009")
        self._output = output
        self._source = source
        self._facilitator = facilitator

    @property
    @override
    def description(self) -> str:
        amount = self._output.amount
        return (
            f"send {amount.amount} {amount.token.symbol} to {self._output.address.value}"
            f" via {self._facilitator}"
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
    async def estimate_costs(
        self, evm_for: EvmFor, job_address: ChecksumAddress
    ) -> tuple[TokenAmount, ...]:
        return ()  # the facilitator pays the gas, and facilitators are free

    @override
    async def run(self, ctx: OperationContext) -> None:
        amount = self._output.amount
        token = amount.token
        contract = to_checksum_address(str(token.contract))
        evm = ctx.evm(self.chain)
        name, version = await evm.eip712_domain(token)

        now = int(Timestamp.now().dt.timestamp())
        nonce = Hex32.from_bytes(os.urandom(32))
        authorization = TransferAuthorization(
            sender=ctx.signer.address,
            recipient=to_checksum_address(self._output.address.value),
            value=amount.amount,
            valid_after=now - 60,
            valid_before=now + AUTHORIZATION_TTL_SECONDS,
            nonce=nonce,
        )
        signature = authorization.sign(ctx.signer, (name, version), evm.chain_id, contract)
        requirements = PaymentRequirements(
            scheme="exact",
            network=self.chain.caip2,
            amount=amount.amount,
            asset=contract,
            payTo=authorization.recipient,
            maxTimeoutSeconds=AUTHORIZATION_TTL_SECONDS,
            extra={"name": name, "version": version},
        )
        payload = PaymentPayload(
            x402Version=X402_VERSION,
            resource=ResourceInfo(
                url=f"teeswap:invoice/{ctx.invoice.id}",
                description=self.description,
                mimeType="application/json",
            ),
            accepted=requirements,
            payload={"signature": "0x" + signature.hex(), "authorization": authorization.wire()},
        )
        in_flight = Place(address=self._output.address, custody=Custody.IN_FLIGHT, reference=nonce)

        # intent first: once recorded, the funds are accounted for by the authorization nonce
        ctx.report(_movement(MovementKind.TRANSIT, amount, self._source, in_flight, None))
        try:
            tx = await self._facilitate(ctx.client, payload, requirements)
        except (OperationError, facilitator.FacilitatorError) as e:
            # the outcome is unclear: the token knows whether the authorization was used
            if await evm.authorization_used(contract, ctx.signer.address, nonce):
                delivered = Place(
                    address=self._output.address, custody=Custody.DELIVERED, reference=nonce
                )
                ctx.report(_movement(MovementKind.OUTPUT, amount, in_flight, delivered, None))
                return
            ctx.report(_movement(MovementKind.TRANSIT, amount, in_flight, self._source, None))
            ctx.exclude_facilitator(self._facilitator)
            raise OperationError(str(e)) from e
        delivered = Place(address=self._output.address, custody=Custody.DELIVERED, reference=tx)
        ctx.report(_movement(MovementKind.OUTPUT, amount, in_flight, delivered, tx))

    async def _facilitate(
        self, client: BaseHttpClient, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> TxHash:
        verified = await facilitator.verify(client, self._facilitator, payload, requirements)
        if not verified.isValid:
            raise OperationError(
                f"{self._facilitator} rejected the transfer: {verified.invalidReason}"
            )
        settled = await facilitator.settle(client, self._facilitator, payload, requirements)
        if not settled.success:
            reason = settled.errorReason or settled.errorMessage
            raise OperationError(f"{self._facilitator} failed to settle: {reason}")
        return TxHash(settled.transaction)


def _movement(
    kind: MovementKind,
    amount: TokenAmount,
    source: Place,
    destination: Place,
    transaction: TxHash | None,
) -> Movement:
    return Movement(
        kind=kind,
        amount=amount,
        source=source,
        destination=destination,
        timestamp=Timestamp.now(),
        transaction=transaction,
    )
