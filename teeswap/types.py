import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, ClassVar, Self, override

import dacite
from eth_typing import Hash32
from litestar.params import Parameter

from .blockchain.chains import Chain
from .response import DataclassResponse

# --- Base mixins ---


class Validated:
    registry: ClassVar[list[type]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        Validated.registry.append(cls)


class HasFromDict:
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return dacite.from_dict(
            data_class=cls,
            data=data,  # pyrefly: ignore[bad-argument-type]
            config=dacite.Config(strict=True, cast=Validated.registry),
        )


# --- Validated newtypes ---


class HexStr(str, Validated):
    LENGTH: int | None = None

    def __new__(cls, value: str) -> HexStr:
        v = str(value)
        try:
            raw = bytes.fromhex(v.removeprefix("0x"))
        except ValueError:
            raise ValueError(f"invalid hex string: {v!r}") from None
        if cls.LENGTH is not None and len(raw) != cls.LENGTH:
            raise ValueError(f"{cls.__name__}: expected {cls.LENGTH} bytes, got {len(raw)}")
        return super().__new__(cls, v)


class HexEd25519PublicKey(HexStr):
    LENGTH = 32


# --- Duration types ---


class Millis(int):
    def to_seconds(self) -> Seconds:
        return Seconds(self / 1000)


class Seconds(float):
    def to_millis(self) -> Millis:
        return Millis(int(self * 1000))


# --- URL with secrets ---


@dataclass(frozen=True, slots=True)
class SecureUrl:
    url: str
    id: str | None = None
    secret_headers: dict[str, str] = field(default_factory=dict)
    url_secrets: dict[str, str] = field(default_factory=dict)

    @override
    def __str__(self) -> str:
        return self.id or self.url


# --- Domain types ---


@dataclass(frozen=True, slots=True)
class Token:
    symbol: str
    chain: Chain
    contract: str | None
    decimals: int


@dataclass(frozen=True, slots=True)
class TokenAmount:
    token: Token
    amount: int


class AmountQualifier(enum.StrEnum):
    EXACT = "exact"
    AT_LEAST = "at_least"
    BEST_RATE = "best_rate"


@dataclass(frozen=True, slots=True)
class TokenRequirement:
    token: Token
    amount: int
    qualifier: AmountQualifier
    tolerance_percent: float | None = None


@dataclass(frozen=True, slots=True)
class Transaction:
    chain: Chain
    hash: Hash32
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class HttpExchange:
    timestamp: datetime
    method: str
    url: str
    request_headers: dict[str, str]
    request_body: bytes | None
    status_code: int
    response_headers: dict[str, str]
    response_body: bytes
    latency: Millis


class ProtocolClass(enum.StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


# --- Quote / Invoice request types ---


@dataclass(frozen=True, slots=True)
class Output:
    token: Annotated[Token, Parameter(description="Token to deliver")]
    amount: Annotated[int, Parameter(description="Amount in smallest units")]
    recipient: Annotated[str, Parameter(description="Destination address")]


@dataclass(frozen=True, slots=True)
class QuoteRequest(HasFromDict):
    input: Annotated[TokenAmount, Parameter(description="What the user is depositing")]
    outputs: Annotated[tuple[Output, ...], Parameter(description="Where to send the results")]
    tolerance_percent: Annotated[
        float, Parameter(description="Acceptable slippage as a percentage", ge=0, le=10)
    ] = 0.5


@dataclass(frozen=True, slots=True)
class GasEstimate:
    token: Token
    amount: Annotated[int, Parameter(description="Estimated gas cost in smallest units")]


@dataclass(frozen=True, slots=True)
class OutputEstimate:
    recipient: str
    token: Token
    amount: Annotated[int, Parameter(description="Estimated amount after gas and fees")]


@dataclass(frozen=True)
class QuoteResponse(DataclassResponse):
    quote_id: Annotated[str, Parameter(description="Use this ID with teeswap_accept")]
    input: TokenAmount
    outputs: tuple[OutputEstimate, ...]
    gas: GasEstimate
    fee: TokenAmount
    expires_at: Annotated[datetime, Parameter(description="Quote expires at this time (UTC)")]


@dataclass(frozen=True, slots=True)
class AcceptRequest(HasFromDict):
    quote_id: Annotated[str, Parameter(description="Quote ID from teeswap_quote")]


@dataclass(frozen=True, slots=True)
class DepositInstruction:
    address: Annotated[str, Parameter(description="Send funds to this address")]
    amount: Annotated[TokenAmount, Parameter(description="Exact amount to deposit")]
    chain: Annotated[Chain, Parameter(description="Chain to deposit on")]


@dataclass(frozen=True)
class AcceptResponse(DataclassResponse):
    quote_id: str
    deposits: Annotated[
        tuple[DepositInstruction, ...], Parameter(description="What to deposit and where")
    ]
    expires_at: Annotated[datetime, Parameter(description="Deposits must arrive before this time")]
    instructions: Annotated[str, Parameter(description="Human-readable deposit instructions")]


@dataclass(frozen=True, slots=True)
class StatusRequest(HasFromDict):
    quote_id: Annotated[str, Parameter(description="Quote/invoice ID")]
