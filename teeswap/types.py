import enum
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, ClassVar, Self, override

import dacite
from eth_utils.address import is_checksum_address
from litestar.params import Parameter

from .blockchain.chains import Chain, ChainFamily
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
    """Hex-encoded bytes, stored canonically as lowercase with a 0x prefix.

    pattern() is the single definition of what is accepted: the constructor
    matches against it and the JSON Schema publishes it, so the two cannot drift.
    """

    LENGTH: ClassVar[int | None] = None

    @classmethod
    def pattern(cls) -> str:
        count = "*" if cls.LENGTH is None else f"{{{cls.LENGTH}}}"
        return f"^(0x)?(?:[0-9a-fA-F]{{2}}){count}$"

    def __new__(cls, value: str) -> Self:
        # dacite's cast hands over raw JSON values; str() would turn 1234 into valid hex
        if not isinstance(value, str):
            raise TypeError(f"{cls.__name__}: expected str, got {type(value).__name__}")
        if re.fullmatch(cls.pattern(), value) is None:
            raise ValueError(f"{cls.__name__}: not a match for {cls.pattern()}: {value!r}")
        return super().__new__(cls, "0x" + value.removeprefix("0x").lower())

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        return cls(raw.hex())


class TxHash(HexStr):
    LENGTH = 32


# --- Duration types ---


class Millis(int):
    pass


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


type Url = SecureUrl | str


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


@dataclass(frozen=True, slots=True)
class Address:
    """An account on a specific chain (CAIP-10)."""

    chain: Chain
    value: str

    def __post_init__(self) -> None:
        if self.chain.family == ChainFamily.EVM and not is_checksum_address(self.value):
            raise ValueError(f"not an EIP-55 checksummed address: {self.value!r}")


@dataclass(frozen=True, slots=True)
class Balance:
    amount: TokenAmount
    address: Address

    def __post_init__(self) -> None:
        if self.amount.token.chain != self.address.chain:
            raise ValueError(
                f"{self.amount.token.symbol} is on {self.amount.token.chain.caip2}, "
                f"address is on {self.address.chain.caip2}"
            )


@dataclass(frozen=True, slots=True)
class Transaction:
    chain: Chain
    hash: TxHash
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
class QuoteRequest(HasFromDict):
    inputs: Annotated[
        tuple[TokenAmount, ...],
        Parameter(description="What the user is depositing; amounts of the same token are summed"),
    ]
    outputs: Annotated[tuple[Balance, ...], Parameter(description="Where to send the results")]
    tolerance_percent: Annotated[
        float, Parameter(description="Acceptable slippage as a percentage", ge=0, le=10)
    ] = 0.5


@dataclass(frozen=True)
class QuoteResponse(DataclassResponse):
    quote_id: Annotated[str, Parameter(description="Use this ID with teeswap_accept")]
    inputs: Annotated[tuple[TokenAmount, ...], Parameter(description="Inputs, one per token")]
    outputs: Annotated[
        tuple[Balance, ...], Parameter(description="Estimated amounts after gas and fees")
    ]
    gas: Annotated[TokenAmount, Parameter(description="Estimated gas cost")]
    fee: TokenAmount
    expires_at: Annotated[datetime, Parameter(description="Quote expires at this time (UTC)")]


@dataclass(frozen=True, slots=True)
class InvoiceRequest(HasFromDict):
    quote_id: Annotated[str, Parameter(description="Quote ID from teeswap_quote")]


@dataclass(frozen=True)
class AcceptResponse(DataclassResponse):
    quote_id: str
    deposits: Annotated[tuple[Balance, ...], Parameter(description="What to deposit and where")]
    expires_at: Annotated[datetime, Parameter(description="Deposits must arrive before this time")]
    instructions: Annotated[str, Parameter(description="Human-readable deposit instructions")]
