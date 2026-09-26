"""x402 v2 wire types and transport constants (see docs/X402.md).

Field names are the spec's. Transport-neutral: a paid tool raises PaymentRequiredError
and, once paid, returns a PaidResponse; the MCP and HTTP layers add the resource (they
know where the call was made) and render both in their own transport.
"""

import abc
import os
from dataclasses import dataclass, field
from typing import Any, override

from eth_utils.address import to_checksum_address

from .blockchain.evm import EthSigner, TransferAuthorization
from .common import TeeSwapError
from .response import ToolResponse
from .types import Amount, Hex32, Timestamp
from .wire import WireStruct

X402_VERSION = 2

# MCP transport (specs/transports-v2/mcp.md)
MCP_PAYMENT_META_KEY = "x402/payment"
MCP_PAYMENT_RESPONSE_META_KEY = "x402/payment-response"

# HTTP transport (specs/transports-v2/http.md): each carries base64-encoded JSON
HTTP_PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
HTTP_PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
HTTP_PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


@dataclass(frozen=True, slots=True)
class ResourceInfo(WireStruct):
    url: str
    description: str
    mimeType: str  # noqa: N815  # x402 wire field name


@dataclass(frozen=True, slots=True)
class PaymentRequirements(WireStruct):
    """One entry of `accepts`: a way the client may pay."""

    scheme: str
    network: str
    amount: Amount
    asset: str
    payTo: str  # noqa: N815  # x402 wire field name
    maxTimeoutSeconds: int  # noqa: N815  # x402 wire field name
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentRequired(WireStruct):
    x402Version: int  # noqa: N815  # x402 wire field name
    error: str
    resource: ResourceInfo
    accepts: tuple[PaymentRequirements, ...]


@dataclass(frozen=True, slots=True)
class PaymentPayload(WireStruct):
    """What the client sends when paying. `payload` is scheme-specific."""

    x402Version: int  # noqa: N815  # x402 wire field name
    resource: ResourceInfo
    accepted: PaymentRequirements
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VerifyResponse(WireStruct):
    isValid: bool  # noqa: N815  # x402 wire field name
    payer: str
    invalidReason: str | None = None  # noqa: N815  # x402 wire field name
    # sent by x402-rs, not in the spec
    invalidReasonDetails: str | None = None  # noqa: N815  # x402-rs wire field name


@dataclass(frozen=True, slots=True)
class SettleResponse(WireStruct):
    success: bool
    payer: str
    transaction: str
    network: str
    errorReason: str | None = None  # noqa: N815  # x402 wire field name
    # sent by x402-rs, not in the spec
    errorMessage: str | None = None  # noqa: N815  # x402-rs wire field name


@dataclass(frozen=True, slots=True)
class PaidResponse[R: ToolResponse](ToolResponse):
    """A paid tool's response, once its payment settled: the transport reports the
    settlement with it (MCP _meta["x402/payment-response"], HTTP PAYMENT-RESPONSE)."""

    response: R
    settlement: SettleResponse

    @override
    def to_mcp_content(self) -> list[dict[str, Any]]:
        return self.response.to_mcp_content()

    @override
    def to_rest(self) -> tuple[Any, str]:
        return self.response.to_rest()


class PaymentError(TeeSwapError):
    pass


class PaymentRequiredError(PaymentError):
    """A paid tool asks to be paid, first or again (`error` says why the last payment
    wasn't taken). The tool knows what to pay; the transport adds the resource, since
    only it knows where the call was made, and sends x402's PaymentRequired."""

    # `accepts` has a default so a client can rebuild it from its code and message alone
    def __init__(self, error: str, accepts: tuple[PaymentRequirements, ...] = ()) -> None:
        super().__init__(error)
        self.error = error
        self.accepts = accepts

    def required(self, resource: ResourceInfo) -> PaymentRequired:
        return PaymentRequired(
            x402Version=X402_VERSION, error=self.error, resource=resource, accepts=self.accepts
        )


class PaymentNotSettledError(PaymentError):
    """A payment was sent and not settled; the resource asks to be paid again."""


# --- Paying (the client side) ---


class Payer(abc.ABC):
    """What pays for a resource: picks a requirement it can fund, and signs it."""

    @abc.abstractmethod
    async def pay(self, required: PaymentRequired) -> PaymentPayload: ...


@dataclass(frozen=True, slots=True)
class SignedPayment:
    """An `exact` EVM payment: the payload to send, and the EIP-3009 authorization in it."""

    payload: PaymentPayload
    authorization: TransferAuthorization
    signature: bytes

    @classmethod
    def from_payload(cls, payload: PaymentPayload) -> SignedPayment:
        """A payer's `exact` EVM payment, read back from the payload they sent."""
        fields = payload.payload.get("authorization")
        signature = payload.payload.get("signature")
        if not isinstance(fields, dict) or not isinstance(signature, str):
            raise PaymentError("an exact EVM payment needs an authorization and a signature")
        strings = {str(k): v for k, v in fields.items() if isinstance(v, str)}
        try:
            authorization = TransferAuthorization.from_wire(strings)
            raw_signature = bytes.fromhex(signature.removeprefix("0x"))
        except (KeyError, ValueError) as e:
            raise PaymentError(f"malformed exact EVM payment: {e}") from e
        return cls(payload=payload, authorization=authorization, signature=raw_signature)


def sign_exact_evm(
    signer: EthSigner,
    resource: ResourceInfo,
    requirements: PaymentRequirements,
    valid_after: int,
    valid_before: int,
    nonce: Hex32,
) -> SignedPayment:
    """Sign `requirements` (scheme `exact`, an eip155 network) as an EIP-3009 transfer.

    The token's signing domain comes with the requirements, in `extra`.
    """
    family, _, reference = requirements.network.partition(":")
    name = requirements.extra.get("name")
    version = requirements.extra.get("version")
    if requirements.scheme != "exact" or family != "eip155":
        raise PaymentError(
            f"not an exact EVM payment: {requirements.scheme} on {requirements.network}"
        )
    if not isinstance(name, str) or not isinstance(version, str):
        raise PaymentError("exact EVM requirements need the token's name and version in extra")
    authorization = TransferAuthorization(
        sender=signer.address,
        recipient=to_checksum_address(requirements.payTo),
        value=requirements.amount,
        valid_after=valid_after,
        valid_before=valid_before,
        nonce=nonce,
    )
    token = to_checksum_address(requirements.asset)
    signature = authorization.sign(signer, (name, version), int(reference), token)
    payload = PaymentPayload(
        x402Version=X402_VERSION,
        resource=resource,
        accepted=requirements,
        payload={"signature": "0x" + signature.hex(), "authorization": authorization.wire()},
    )
    return SignedPayment(payload=payload, authorization=authorization, signature=signature)


class EvmPayer(Payer):
    """Pays `exact` requirements on EVM networks (EIP-3009) from one key.

    A payment stays usable for as long as the resource allows (maxTimeoutSeconds), or
    for `valid_for` seconds if that's sooner: after that, nobody can settle it.
    """

    def __init__(self, signer: EthSigner, valid_for: int | None = None) -> None:
        self._signer = signer
        self._valid_for = valid_for

    @property
    def address(self) -> str:
        return self._signer.address

    @override
    async def pay(self, required: PaymentRequired) -> PaymentPayload:
        for requirements in required.accepts:
            if requirements.scheme == "exact" and requirements.network.startswith("eip155:"):
                now = int(Timestamp.now().dt.timestamp())
                valid_for = requirements.maxTimeoutSeconds
                if self._valid_for is not None:
                    valid_for = min(valid_for, self._valid_for)
                return sign_exact_evm(
                    self._signer,
                    required.resource,
                    requirements,
                    valid_after=now - 60,
                    valid_before=now + valid_for,
                    nonce=Hex32.from_bytes(os.urandom(32)),
                ).payload
        raise PaymentError("none of the payment requirements can be paid from an EVM key")
