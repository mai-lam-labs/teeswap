"""x402 v2 wire types and transport constants (see docs/X402.md).

Field names are the spec's. Transport-neutral: a PaidTool returns X402PaymentSpec;
the MCP and HTTP layers add the resource (they know where the call was made)
and render the PaymentRequired in their own transport.
"""

from dataclasses import dataclass, field
from typing import Any

from .response import ToolResponse
from .types import Amount
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
class X402PaymentSpec:
    """What a PaidTool returns when it needs paying: the transport adds the resource."""

    error: str
    accepts: tuple[PaymentRequirements, ...]

    def required(self, resource: ResourceInfo) -> PaymentRequired:
        return PaymentRequired(
            x402Version=X402_VERSION, error=self.error, resource=resource, accepts=self.accepts
        )


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
class X402PaymentResult:
    """What a PaidTool returns when paid and settled: the transport reports the
    settlement (MCP _meta["x402/payment-response"], HTTP PAYMENT-RESPONSE)."""

    response: ToolResponse
    settlement: SettleResponse
