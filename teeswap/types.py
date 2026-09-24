import functools
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, ClassVar, Self, override

from eth_utils.address import is_checksum_address
from litestar.params import Parameter

from .blockchain.chains import Chain, ChainFamily
from .response import DataclassResponse
from .wire import Encodable, Validated, WireSchema, WireStruct

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

    def to_bytes(self) -> bytes:
        return bytes.fromhex(self.removeprefix("0x"))

    @override
    def to_wire(self) -> Encodable:
        return str(self)

    @override
    @classmethod
    def json_schema(cls) -> WireSchema:
        size = "" if cls.LENGTH is None else f"{cls.LENGTH}-byte "
        return WireSchema(
            type="string", pattern=cls.pattern(), description=f"{size}hex string, 0x-prefixed"
        )


class Hex32(HexStr):
    LENGTH = 32


class TxHash(Hex32):
    pass


class Amount(int, Validated):
    """A non-negative integer amount in a token's smallest units.

    On the wire it is a decimal string: JavaScript numbers lose precision past 2**53.
    """

    PATTERN: ClassVar[str] = "^[0-9]+$"

    def __new__(cls, value: int | str) -> Self:
        if isinstance(value, bool):
            raise TypeError("Amount: expected int or decimal string, got bool")
        if isinstance(value, str) and re.fullmatch(cls.PATTERN, value) is None:
            raise ValueError(f"Amount: not a decimal integer string: {value!r}")
        if not isinstance(value, int | str):
            raise TypeError(f"Amount: expected int or decimal string, got {type(value).__name__}")
        amount = int(value)
        if amount < 0:
            raise ValueError(f"Amount: must be non-negative, got {amount}")
        return super().__new__(cls, amount)

    @override
    def to_wire(self) -> Encodable:
        return str(int(self))

    @override
    @classmethod
    def json_schema(cls) -> WireSchema:
        return WireSchema(
            type="string",
            pattern=cls.PATTERN,
            description="integer amount in smallest units, as a decimal string",
        )


@functools.total_ordering
class Timestamp(Validated):
    """A UTC instant with whole-second precision, wrapping a datetime.

    On the wire it is RFC 3339, "YYYY-MM-DDTHH:MM:SSZ". Truncating to seconds at
    construction means encoding never loses anything, so it round-trips exactly.
    """

    FORMAT: ClassVar[str] = "%Y-%m-%dT%H:%M:%SZ"
    PATTERN: ClassVar[str] = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"

    def __init__(self, value: datetime | str) -> None:
        if isinstance(value, str):
            if re.fullmatch(self.PATTERN, value) is None:
                raise ValueError(f"Timestamp: expected YYYY-MM-DDTHH:MM:SSZ, got {value!r}")
            parsed = datetime.strptime(value, self.FORMAT).replace(tzinfo=UTC)
        elif isinstance(value, datetime):
            if value.utcoffset() != timedelta(0):
                raise ValueError(f"Timestamp: datetime must be UTC, got {value!r}")
            parsed = value
        else:
            raise TypeError(f"Timestamp: expected datetime or str, got {type(value).__name__}")
        self._dt = parsed.replace(microsecond=0, tzinfo=UTC)

    @classmethod
    def now(cls) -> Self:
        return cls(datetime.now(UTC))

    @property
    def dt(self) -> datetime:
        return self._dt

    def __add__(self, delta: timedelta) -> Timestamp:
        return Timestamp(self._dt + delta)

    def __sub__(self, other: Timestamp) -> timedelta:
        return self._dt - other._dt

    @override
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Timestamp) and self._dt == other._dt

    def __lt__(self, other: Timestamp) -> bool:
        return self._dt < other._dt

    @override
    def __hash__(self) -> int:
        return hash(self._dt)

    @override
    def __repr__(self) -> str:
        return f"Timestamp({self.to_wire()!r})"

    @override
    def to_wire(self) -> Encodable:
        return self._dt.strftime(self.FORMAT)

    @override
    @classmethod
    def json_schema(cls) -> WireSchema:
        return WireSchema(
            type="string",
            format="date-time",
            pattern=cls.PATTERN,
            description="UTC time, RFC 3339 with whole seconds",
        )


class Percent(Decimal, Validated):
    """A percentage from 0 to 100 (0.5 means 0.5%). On the wire it is a JSON number."""

    def __new__(cls, value: Decimal | int | str) -> Self:
        if isinstance(value, bool | float):
            raise TypeError(f"Percent: expected Decimal, int or str, got {type(value).__name__}")
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"Percent: not a number: {value!r}") from None
        if not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(100):
            raise ValueError(f"Percent: must be between 0 and 100, got {value!r}")
        return super().__new__(cls, parsed)

    @override
    def to_wire(self) -> Encodable:
        return Decimal(self)

    @override
    @classmethod
    def json_schema(cls) -> WireSchema:
        return WireSchema(type="number", minimum=0, maximum=100, description="percentage")


# --- Duration types ---


class Millis(int):
    pass


# --- URL with secrets ---


@dataclass(frozen=True, slots=True)
class SecureUrl(WireStruct):
    url: str
    id: str | None = None
    secret_headers: dict[str, str] = field(default_factory=dict)
    url_secrets: dict[str, str] = field(default_factory=dict)

    @override
    def __str__(self) -> str:
        return self.id or self.url


# a plain union, not a `type` alias: dacite can't see through TypeAliasType in hydrated fields
Url = SecureUrl | str


# --- Domain types ---


@dataclass(frozen=True, slots=True)
class Token(WireStruct):
    symbol: str
    chain: Chain
    contract: str | None
    decimals: int

    @classmethod
    def native(cls, chain: Chain) -> Token:
        """The chain's native token (what gas is paid in)."""
        return cls(
            symbol=chain.native_token, chain=chain, contract=None, decimals=chain.native_decimals
        )


@dataclass(frozen=True, slots=True)
class TokenAmount(WireStruct):
    token: Token
    amount: Amount


@dataclass(frozen=True, slots=True)
class Address(WireStruct):
    """An account on a specific chain (CAIP-10)."""

    chain: Chain
    value: str

    def __post_init__(self) -> None:
        if self.chain.family == ChainFamily.EVM and not is_checksum_address(self.value):
            raise ValueError(f"not an EIP-55 checksummed address: {self.value!r}")


@dataclass(frozen=True, slots=True)
class Balance(WireStruct):
    amount: TokenAmount
    address: Address

    def __post_init__(self) -> None:
        if self.amount.token.chain != self.address.chain:
            raise ValueError(
                f"{self.amount.token.symbol} is on {self.amount.token.chain.caip2}, "
                f"address is on {self.address.chain.caip2}"
            )


@dataclass(frozen=True, slots=True)
class Transaction(WireStruct):
    chain: Chain
    hash: TxHash
    timestamp: Timestamp


@dataclass(frozen=True, slots=True)
class HttpExchange:
    timestamp: Timestamp
    method: str
    url: str
    request_headers: dict[str, str]
    request_body: bytes | None
    status_code: int
    response_headers: dict[str, str]
    response_body: bytes
    latency: Millis


# --- Quote / Invoice request types ---

DEFAULT_TOLERANCE = Percent("0.5")


@dataclass(frozen=True, slots=True)
class QuoteRequest(WireStruct):
    inputs: Annotated[
        tuple[TokenAmount, ...],
        Parameter(description="What the user is depositing; amounts of the same token are summed"),
    ]
    outputs: Annotated[tuple[Balance, ...], Parameter(description="Where to send the results")]
    tolerance_percent: Annotated[
        Percent, Parameter(description="Acceptable slippage as a percentage")
    ] = DEFAULT_TOLERANCE


@dataclass(frozen=True, slots=True)
class HeldInput(WireStruct):
    """An input already in an account its owner holds the key to: what the account holds
    of `token` is the input."""

    token: Token
    private_key: Annotated[Hex32, Parameter(description="The account's key: a secret")]


@dataclass(frozen=True, slots=True)
class KeysQuoteRequest(WireStruct):
    inputs: Annotated[
        tuple[HeldInput, ...],
        Parameter(description="The accounts holding the inputs, one per token, with their keys"),
    ]
    outputs: Annotated[tuple[Balance, ...], Parameter(description="Where to send the results")]
    tolerance_percent: Annotated[
        Percent, Parameter(description="Acceptable slippage as a percentage")
    ] = DEFAULT_TOLERANCE


@dataclass(frozen=True)
class QuoteResponse(DataclassResponse):
    quote_id: Annotated[str, Parameter(description="Use this ID with teeswap_accept")]
    inputs: Annotated[tuple[TokenAmount, ...], Parameter(description="Inputs, one per token")]
    outputs: Annotated[tuple[Balance, ...], Parameter(description="Estimated amounts after gas")]
    gas: Annotated[TokenAmount, Parameter(description="Estimated gas cost")]
    plan: Annotated[
        tuple[str, ...],
        Parameter(description="Provisional plan: what Mai expects to do (may change)"),
    ]
    expires_at: Annotated[Timestamp, Parameter(description="Quote expires at this time")]


@dataclass(frozen=True, slots=True)
class InvoiceRequest(WireStruct):
    quote_id: Annotated[str, Parameter(description="Quote ID from teeswap_quote")]


@dataclass(frozen=True)
class AcceptResponse(DataclassResponse):
    quote_id: str
    deposits: Annotated[tuple[Balance, ...], Parameter(description="What to deposit and where")]
    expires_at: Annotated[Timestamp, Parameter(description="Deposits must arrive before this time")]
    instructions: Annotated[str, Parameter(description="Human-readable deposit instructions")]
