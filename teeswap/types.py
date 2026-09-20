import enum
from dataclasses import dataclass, field
from typing import Annotated

from litestar.params import Parameter


class ChainFamily(enum.StrEnum):
    EVM = "evm"
    SOLANA = "solana"


@dataclass(frozen=True, slots=True)
class ChainId:
    name: str
    chain_id: int
    family: ChainFamily
    rpc_url: str


@dataclass(frozen=True, slots=True)
class TokenInfo:
    symbol: str
    address: str
    chain: str
    decimals: int


class RiskPreference(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RouteOrder(enum.StrEnum):
    RECOMMENDED = "RECOMMENDED"
    FASTEST = "FASTEST"
    CHEAPEST = "CHEAPEST"
    SAFEST = "SAFEST"


class ProtocolClass(enum.StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


@dataclass(frozen=True, slots=True)
class RiskProfile:
    score: float
    max_admin_risk: str
    worst_leg_incidents_12mo: int
    has_judgment_oracle_dependency: bool


@dataclass(frozen=True, slots=True)
class Leg:
    protocol: str
    action: str
    chain: str
    protocol_class: ProtocolClass
    estimated_time_seconds: int


@dataclass(frozen=True, slots=True)
class Route:
    legs: tuple[Leg, ...]
    estimated_total_time_seconds: int
    risk_profile: RiskProfile


class InputMethod(enum.StrEnum):
    X402_PASSTHROUGH = "x402-passthrough"
    X402_TO_TEE = "x402-to-tee"
    DIRECT_DEPOSIT = "direct-deposit"


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    input_token: Annotated[str, Parameter(description="Token symbol ('USDC') or contract address")]
    input_chain: Annotated[
        str, Parameter(description="Source chain ('base', 'ethereum', 'arbitrum')")
    ]
    input_amount: Annotated[
        str, Parameter(description="Human-readable decimal amount, not raw wei")
    ]
    output_token: Annotated[str, Parameter(description="Destination token symbol or address")]
    output_chain: Annotated[str, Parameter(description="Destination chain name")]
    recipient: Annotated[
        str, Parameter(description="Destination address (format must match chain)")
    ]
    risk_preference: RiskPreference = RiskPreference.MEDIUM
    order: RouteOrder = RouteOrder.RECOMMENDED
    slippage_bps: Annotated[
        int, Parameter(description="Slippage tolerance in basis points", ge=1, le=500)
    ] = 50
    exclude_protocols: tuple[str, ...] = ()
    max_price_cap: Annotated[
        str | None, Parameter(description="Refuse if total cost exceeds this")
    ] = None


@dataclass(frozen=True, slots=True)
class QuoteResponse:
    quote_id: str
    output_amount: Annotated[
        str, Parameter(description="Estimated output in human-readable decimal")
    ]
    min_output_amount: Annotated[str, Parameter(description="Minimum output after slippage")]
    route: Route
    input_method: InputMethod
    deposit_address: Annotated[
        str | None, Parameter(description="Only set for direct-deposit method")
    ]
    deadline: Annotated[int, Parameter(description="Quote expiry as unix timestamp")]
    execution_payment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecuteRequest:
    quote_id: Annotated[str, Parameter(description="Quote ID from teeswap_quote response")]


@dataclass(frozen=True, slots=True)
class LegStatus:
    step: int
    protocol: str
    chain: str
    tx_hash: str | None
    status: str
    timestamp: int | None


@dataclass(frozen=True, slots=True)
class StatusResponse:
    order_id: str
    status: str
    legs: tuple[LegStatus, ...]
    output_amount: Annotated[
        str | None, Parameter(description="Actual output amount once delivered")
    ]


@dataclass(frozen=True, slots=True)
class StatusRequest:
    order_id: Annotated[str, Parameter(description="Order ID from teeswap_execute response")]


@dataclass(frozen=True, slots=True)
class RoutesFilter:
    input_chain: Annotated[str | None, Parameter(description="Filter by source chain")] = None
    output_chain: Annotated[str | None, Parameter(description="Filter by destination chain")] = None
    input_token: Annotated[str | None, Parameter(description="Filter by source token")] = None
    output_token: Annotated[str | None, Parameter(description="Filter by destination token")] = None


@dataclass(frozen=True, slots=True)
class RefundRequest:
    order_id: Annotated[str, Parameter(description="Order ID of the failed/timed-out swap")]


@dataclass(frozen=True, slots=True)
class RefundResponse:
    order_id: str
    status: str
    tx_hash: str | None
    refund_amount: str | None


@dataclass(frozen=True, slots=True)
class InvoiceListRequest:
    pass


@dataclass(frozen=True, slots=True)
class InvoiceDetailRequest:
    invoice_id: Annotated[str, Parameter(description="Invoice ID")]


@dataclass(frozen=True, slots=True)
class InvoicePayRequest:
    invoice_id: Annotated[str, Parameter(description="Invoice ID to pay")]
    authorization: Annotated[str, Parameter(description="Signed x402 payment authorization")]
    chain_id: Annotated[int, Parameter(description="Chain ID for the payment")]
    payer: Annotated[str, Parameter(description="Payer address")]
