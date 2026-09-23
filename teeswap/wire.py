"""Wire format: the one way data enters (HasFromDict) and leaves (encode) as JSON.

Decoding: parse_json/decode_object parse bytes (numbers with a fraction become
Decimal, never float); HasFromDict.from_dict hydrates dataclasses from the result.

Encoding: encode() writes canonical JSON (sorted keys, compact, raw UTF-8). Typed
objects are encodable only through HasToWire.to_wire(): scalars via Validated,
dataclasses via WireStruct. The encoder itself only knows JSON primitives, lists,
tuples and str-keyed mappings. Anything else raises. The same bytes are used for responses and for commitments (jcs).

Round trip: for every wire type T, T.from_dict(decode_object(encode(x))) == x.
"""

import abc
import enum
import json
from collections.abc import Mapping
from dataclasses import Field, dataclass, fields
from decimal import Decimal
from typing import Any, ClassVar, NoReturn, Self, override

import dacite

from .common import TeeSwapError

# What encode() accepts. Typed objects must opt in via HasToWire (dataclasses via WireStruct);
# the runtime checks in _write mirror this, and values typed Any (parsed JSON, tool arguments)
# still reach the final raise if they hold anything else.
type Encodable = (
    HasToWire
    | bool
    | int
    | str
    | Decimal
    | list[Encodable]
    | tuple[Encodable, ...]
    | Mapping[str, Encodable]
    | None
)


class WireError(TeeSwapError):
    pass


# --- Types that control their own wire form ---


class HasToWire(abc.ABC):
    @abc.abstractmethod
    def to_wire(self) -> Encodable: ...


@dataclass(frozen=True, slots=True)
class WireSchema:
    """JSON Schema for a scalar wire type, published by schema.ValidatedSchemaPlugin."""

    type: str
    description: str
    format: str | None = None
    pattern: str | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None


class Validated(HasToWire):
    """A scalar wire type: __new__ parses the wire form, to_wire() produces it."""

    registry: ClassVar[list[type]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        Validated.registry.append(cls)

    @classmethod
    @abc.abstractmethod
    def json_schema(cls) -> WireSchema: ...


# --- Decoding ---


def _reject_constant(name: str) -> NoReturn:
    raise WireError(f"{name} is not valid JSON")


def parse_json(raw: bytes | str) -> Any:
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
    except ValueError as e:
        raise WireError(f"invalid JSON: {e}") from e


def decode_object(raw: bytes | str) -> dict[str, Any]:
    data = parse_json(raw)
    if not isinstance(data, dict):
        raise WireError("expected a JSON object")
    return data


class HasFromDict:
    """The standard way to hydrate a dataclass from a dict (parsed JSON, TOML, ...).

    Every type built from external data inherits this, so hydration rules
    (strict keys, Validated casts) are the same everywhere.
    """

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return dacite.from_dict(
            data_class=cls,
            # dacite's Data protocol declares __getitem__(*args, **kwargs) instead of
            # (key: str), so pyrefly rejects dict[str, Any]; this is the one place it's suppressed
            data=data,  # pyrefly: ignore[bad-argument-type]
            # JSON has no enums or tuples: cast str -> StrEnum and list -> tuple
            config=dacite.Config(strict=True, cast=[*Validated.registry, enum.Enum, tuple]),
        )


class WireStruct(HasFromDict, HasToWire):
    """A dataclass that crosses the wire: hydrated by from_dict, encoded field by field.

    Dataclasses are only encodable by inheriting this, so internal state (an Invoice
    holding its signer) can never be serialized by accident. Override to_wire for a
    different shape.
    """

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    @override
    def to_wire(self) -> Encodable:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# --- Encoding ---


def encode(value: Encodable) -> bytes:
    out: list[str] = []
    _write(value, out)
    return "".join(out).encode()


def _write(value: Encodable, out: list[str]) -> None:
    if isinstance(value, HasToWire):
        _write(value.to_wire(), out)
    elif value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, int):
        out.append(str(int(value)))
    elif isinstance(value, str):
        out.append(json.dumps(str(value), ensure_ascii=False))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise WireError(f"cannot encode non-finite Decimal {value}")
        out.append(format(value.normalize(), "f"))
    elif isinstance(value, Mapping):
        _write_object(value, out)
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise WireError(f"cannot encode {type(value).__name__}")


def _write_object(obj: Mapping[Any, Any], out: list[str]) -> None:
    for key in obj:
        if not isinstance(key, str):
            raise WireError(f"object keys must be str, got {type(key).__name__}")
    out.append("{")
    for i, key in enumerate(sorted(obj)):
        if i:
            out.append(",")
        out.append(json.dumps(key, ensure_ascii=False))
        out.append(":")
        _write(obj[key], out)
    out.append("}")
