"""Side effects: what Mai does to the outside world (see docs/EXECUTION.md).

A side effect is one act of reaching out: a transaction broadcast to an RPC, an
authorization submitted to a facilitator. It is recorded on its work-log step
before it is performed, with everything needed to perform it again or check it.

Side effects that repeat the same act share an idempotency key, which the
outside world enforces: a transaction's (account, nonce), an EIP-3009
authorization's (token, authorizer, nonce). However many times it was sent, at
most one of them takes effect. "Did it take effect?" is asked per key, of the
chain or service that enforces it, and the outcome is one of:

- landed: it took effect, with the evidence (reverted: it took effect, and failed).
- void: it never can; whatever it would have moved is still where it was.
- pending: it still might. Keep watching; never replace it with a new act.

A failed send is not void: a request that errored or timed out may still have
been carried out. Only the enforcer of the key can say it wasn't.
"""

import abc
import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self, override

from eth_typing import Hash32
from eth_utils.address import to_checksum_address

from ..blockchain.chains import Chain
from ..blockchain.evm import EvmChain, SignedTransaction, TransferAuthorization
from ..common import TeeSwapError
from ..http import BaseHttpClient
from ..types import Address, Amount, Hex32, Timestamp, Token, TokenAmount, TxHash, Url
from ..wire import WireStruct

type RpcUrlFor = Callable[[Chain], Url]


class SideEffectKind(enum.StrEnum):
    TRANSACTION = "transaction"  # a signed transaction broadcast to an RPC node
    AUTHORIZATION = "authorization"  # an EIP-3009 authorization submitted to a facilitator


# --- Idempotency keys ---


class IdempotencyKey(abc.ABC):
    """What the outside world enforces at most once. Side effects with equal keys are the
    same act sent again, and at most one of them takes effect."""

    @property
    @abc.abstractmethod
    def label(self) -> str:
        """What the key is, for the work log: public values only."""


@dataclass(frozen=True, slots=True)
class TransactionKey(IdempotencyKey):
    """At most one transaction per account and nonce is ever mined."""

    account: Address
    nonce: int

    @property
    @override
    def label(self) -> str:
        return f"{self.account.value} nonce {self.nonce}"


@dataclass(frozen=True, slots=True)
class AuthorizationKey(IdempotencyKey):
    """An EIP-3009 token uses each authorizer's nonce at most once."""

    token: Address
    authorizer: Address
    nonce: Hex32

    @property
    @override
    def label(self) -> str:
        return f"{self.authorizer.value} nonce {self.nonce} on {self.token.value}"


# --- Outcomes ---


class OutcomeStatus(enum.StrEnum):
    PENDING = "pending"
    LANDED = "landed"
    REVERTED = "reverted"  # landed, and failed
    VOID = "void"


class OutcomeError(TeeSwapError):
    pass


@dataclass(frozen=True, slots=True)
class Outcome(WireStruct):
    """Whether a side effect took effect, and the evidence."""

    status: OutcomeStatus
    # the transaction that carried it, when known: never searched for, only reported
    # (by a receipt, or by the facilitator that settled it)
    transaction: TxHash | None = None
    gas: TokenAmount | None = None  # what the job paid for it, if it paid
    reason: str | None = None  # why it is void

    def __post_init__(self) -> None:
        if (self.reason is not None) != (self.status is OutcomeStatus.VOID):
            raise OutcomeError(f"a reason is given for void outcomes only, not {self.status}")
        if not self.took_effect and (self.transaction is not None or self.gas is not None):
            raise OutcomeError(f"a {self.status} outcome has no transaction or gas")

    @classmethod
    def pending(cls) -> Self:
        return cls(status=OutcomeStatus.PENDING)

    @classmethod
    def landed(cls, transaction: TxHash | None, gas: TokenAmount | None = None) -> Self:
        return cls(status=OutcomeStatus.LANDED, transaction=transaction, gas=gas)

    @classmethod
    def reverted(cls, transaction: TxHash, gas: TokenAmount | None) -> Self:
        return cls(status=OutcomeStatus.REVERTED, transaction=transaction, gas=gas)

    @classmethod
    def void(cls, reason: str) -> Self:
        return cls(status=OutcomeStatus.VOID, reason=reason)

    @property
    def resolved(self) -> bool:
        """Final: it took effect, or never can."""
        return self.status is not OutcomeStatus.PENDING

    @property
    def took_effect(self) -> bool:
        return self.status in (OutcomeStatus.LANDED, OutcomeStatus.REVERTED)


# --- Side effects ---


@dataclass(frozen=True, slots=True)
class SideEffectView(WireStruct):
    kind: SideEffectKind
    key: str  # side effects with the same key are one act, sent more than once
    target: str
    sent_at: Timestamp | None
    error: str | None  # the send failed; that alone doesn't mean it didn't happen
    outcome: Outcome


class SideEffect(abc.ABC):
    """One act of reaching the outside world, recorded before it is performed."""

    def __init__(self, target: str) -> None:
        # where it was sent (an RPC node, a facilitator), as a label: never the secrets
        self.target = target
        self.sent_at: Timestamp | None = None
        self.error: str | None = None  # the send failed; that alone doesn't mean it didn't happen
        self.refused = False  # the receiver answered that it won't take it
        self.outcome = Outcome.pending()

    @property
    @abc.abstractmethod
    def kind(self) -> SideEffectKind: ...

    @property
    @abc.abstractmethod
    def key(self) -> IdempotencyKey: ...

    @abc.abstractmethod
    async def check(self, client: BaseHttpClient, rpc_url_for: RpcUrlFor) -> Outcome:
        """Ask whoever enforces the key whether it took effect."""

    def sending(self) -> None:
        self.sent_at = Timestamp.now()

    def failed(self, error: str) -> None:
        self.error = error

    def refuse(self, error: str) -> None:
        self.error = error
        self.refused = True

    def resolve(self, outcome: Outcome) -> None:
        if not outcome.resolved:
            raise OutcomeError("a side effect is resolved by a final outcome, not a pending one")
        self.outcome = outcome

    def view(self) -> SideEffectView:
        return SideEffectView(
            kind=self.kind,
            key=self.key.label,
            target=self.target,
            sent_at=self.sent_at,
            error=self.error,
            outcome=self.outcome,
        )


class TransactionBroadcast(SideEffect):
    """A signed transaction sent to an RPC node. Rebroadcasting sends the same bytes."""

    def __init__(self, chain: Chain, tx: SignedTransaction, rpc: Url) -> None:
        super().__init__(target=str(rpc))
        self.chain = chain
        self.tx = tx

    @property
    @override
    def kind(self) -> SideEffectKind:
        return SideEffectKind.TRANSACTION

    @property
    @override
    def key(self) -> TransactionKey:
        return TransactionKey(account=Address(self.chain, self.tx.sender), nonce=self.tx.nonce)

    @override
    async def check(self, client: BaseHttpClient, rpc_url_for: RpcUrlFor) -> Outcome:
        evm = EvmChain(self.chain, client, rpc_url_for(self.chain))
        # nonce first: if it has moved past ours and ours has no receipt after that, another
        # transaction took the slot and ours can never be mined
        confirmed = await evm.confirmed_nonce(self.tx.sender)
        receipt = await evm.receipt(self.tx.tx_hash)
        if receipt is not None:
            gas = TokenAmount(token=Token.native(self.chain), amount=Amount(receipt.gas_cost))
            tx = _tx_hash(self.tx.tx_hash)
            return Outcome.landed(tx, gas) if receipt.success else Outcome.reverted(tx, gas)
        if confirmed > self.tx.nonce:
            return Outcome.void(f"nonce {self.tx.nonce} was used by another transaction")
        return Outcome.pending()


class AuthorizationSubmission(SideEffect):
    """A signed EIP-3009 authorization handed to a facilitator to settle: Mai's own, or
    a payer's.

    Handing the same authorization to another facilitator is another submission with
    the same key: the token settles it at most once.
    """

    def __init__(
        self,
        chain: Chain,
        token: Token,
        authorization: TransferAuthorization,
        signature: bytes,
        facilitator: str,
    ) -> None:
        super().__init__(target=facilitator)
        self.chain = chain
        self.token = token
        self.authorization = authorization
        self.signature = signature
        self.settled: TxHash | None = None  # the transaction, if the facilitator reported one

    def settled_by(self, transaction: TxHash) -> None:
        self.settled = transaction

    @property
    @override
    def kind(self) -> SideEffectKind:
        return SideEffectKind.AUTHORIZATION

    @property
    @override
    def key(self) -> AuthorizationKey:
        return AuthorizationKey(
            token=Address(self.chain, str(self.token.contract)),
            authorizer=Address(self.chain, self.authorization.sender),
            nonce=self.authorization.nonce,
        )

    @override
    async def check(self, client: BaseHttpClient, rpc_url_for: RpcUrlFor) -> Outcome:
        evm = EvmChain(self.chain, client, rpc_url_for(self.chain))
        contract = to_checksum_address(str(self.token.contract))
        sender = self.authorization.sender
        nonce = self.authorization.nonce
        # time first: if the chain is already past validBefore and the nonce is unused
        # after that, no later block can use it
        block = await evm.latest_block()
        if await evm.authorization_used(contract, sender, nonce):
            return Outcome.landed(self.settled)
        if block.timestamp >= self.authorization.valid_before:
            return Outcome.void("authorization expired unused")
        return Outcome.pending()


def _tx_hash(raw: Hash32) -> TxHash:
    return TxHash.from_bytes(bytes(raw))
