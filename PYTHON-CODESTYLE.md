# TEESwap Python code style

> Strict, honest typing and a straightforward path to idiomatic Rust.
> Where a Python-ism has no clean Rust analogue, prefer the form that does.
> `make check` (ruff + ty + pyrefly + tests) is the gate; keep it green.

## 0. Toolchain

Dev tooling lives under the project: `make install` puts `uv` in `./.uv`
and a `./.venv` with the package installed editable plus dev tools.
Nothing touches `$HOME`.

`make check` = `ruff check` + `ruff format --check` + `ty check` +
`pyrefly check` (strict preset) + `pytest`. All five gates must pass.

Target Python 3.14. Use PEP 695 type parameter syntax (`def f[T]():`),
deferred annotations (PEP 649), and all 3.14 features.

## 1. Modern Python

- Built-in generics and unions: `list[T]`, `dict[K, V]`, `tuple[...]`,
  `X | None`. Never `typing.List`, `Optional`, `Union`.
- PEP 695 generics: `def f[T](cls: type[T]) -> T:` not `TypeVar`.
- `type` statement for type aliases (except in hydrated fields, see §6).
- `StrEnum` for wire values. `@override` on every ABC override.
- `Annotated[T, Parameter(...)]` for Litestar field descriptions.
- `from __future__ import annotations` is unnecessary on 3.14. Do not add.

## 2. Types are strict and honest

The type checker is a design tool, not a formality.

1. No `Any` except at a genuinely dynamic boundary (JSON-RPC params,
   wire format dicts). Bare `dict` is implicit `Any` — pyrefly strict
   rejects it; always specify `dict[str, X]`.
2. The wire-to-typed boundary is `HasFromDict.from_dict` (see §6) —
   never raw dict access with string keys scattered through business logic.
3. Never add type ignore/suppress comments without understanding the
   error and getting explicit confirmation. Each suppression is a
   decision, not boilerplate.

### ABCs for polymorphic interfaces

`Tool` / `PaidTool`, `ToolResponse`, `Operation` — the ABC defines the interface,
each subclass owns its implementation. `@override` on every overridden
method (pyrefly strict enforces this).

`@dataclass(frozen=True, slots=True)` is the default for value types.

### `tuple` over `list` for immutable data

Wire data, function returns, fixed sequences: `tuple`. `list` only when
you actually mutate it.

### `TypedDict` for raw wire format shapes

A JSON structure with known keys gets a `TypedDict` to document and
type-check the expected shape. The typed domain object is a frozen
dataclass hydrated from it via `from_dict`.

## 3. Enums, not string constants

A closed set of values is an enum.

- Values on the wire: `StrEnum` (stable string `.value`).
  Examples: `ProofFormat`, `OrderStatus`, `CloudProvider`, `InvoiceStatus`.
- Typed outcomes: union of frozen dataclasses, not enums.
  Example: `Verified | VerificationFailed`.

## 4. Errors: structural, module-local

Each module owns its error types. Root: `TeeSwapError` in `common.py`.

```
common.py              TeeSwapError
wire.py                  WireError
crypto/attestation.py    AttestationError
crypto/vaportpm.py         VaportpmError / VaportpmParseError
blockchain/evm.py        EvmError
                           SigningError
blockchain/derivation.py UnsupportedChainError / InvalidKeyError
facilitator.py           FacilitatorError
execution/invoice.py     InvoiceError
                           InvoiceNotFoundError / InvoiceExpiredError / InvoiceStateError
                             InvoiceFundingError
execution/accounts.py    AccountError
execution/ledger.py      LedgerError
execution/planner.py     RouteError
                           NoRouteError
execution/quote.py       QuoteError
execution/operations.py  OperationError
execution/engine.py      GuardrailError / X402SettlementError
mcp.py                   ToolNotFoundError / ToolNotAvailableError / InvalidToolArgumentsError
app.py                   RequestError
```

`TeeSwapError` subclasses are domain failures: tools report them to the client
(an `isError` result, a 4xx). Anything else is unexpected: logged, and answered
with `-32603` / 500. `blockchain/rpc.py`'s `JsonRpcError` is deliberately not a
`TeeSwapError`: an upstream RPC failure is unexpected, not the client's doing.

No centralised `errors.py`. Errors live where they're raised.

Result-shaped outcomes are not exceptions. `Verified | VerificationFailed`
is returned, not raised.

## 5. I/O lives at the edges

`mcp.py` handles protocol logic (no HTTP). `app.py` handles HTTP
(no protocol logic). `attestation.py` handles crypto and subprocess
calls. Business logic modules take typed values and return typed values.

A function that both computes and serves is the smell.

## 6. Serialization and schemas

Types are defined once as dataclasses with `Annotated[T, Parameter(...)]`
for descriptions. Litestar's `SchemaCreator` derives JSON Schema from
these types. MCP tool definitions derive `inputSchema` from the same
types via `schema_for_type()`.

All wire handling lives in `wire.py`; Litestar only routes and receives bytes.

- **In:** `decode_object`/`parse_json` parse JSON (fractional numbers become
  `Decimal`, never `float`), and anything built from a dict inherits
  `HasFromDict` and is hydrated with `T.from_dict(data)`: strict keys,
  `Validated` casts, str → enum, list → tuple. No other code parses dicts
  into typed values.
- **Out:** `encode(obj)` writes every response, error body, pipe message and
  commitment input (`jcs` is `encode`): sorted keys, compact, raw UTF-8,
  `null` for `None`. Its input type `Encodable` lists what it accepts, so
  the type checker rejects anything else; at runtime it raises on anything
  it doesn't know, including `float`, `bytes` and bare `datetime`.
- **Opt-in:** a dataclass crosses the wire only by inheriting `WireStruct`
  (`HasFromDict` + `HasToWire`, encoded field by field; override `to_wire`
  for a different shape). Internal dataclasses, e.g. `Invoice` with its
  signer, are not encodable. Hydrate-only types (MCP params) use
  `HasFromDict` alone.
- **Round trip:** for every wire type, `T.from_dict(decode_object(encode(x))) == x`.
- **Scalar wire types** subclass `Validated`: `__new__` parses the wire form,
  `to_wire()` produces it, `json_schema()` describes it (`ValidatedSchemaPlugin`
  hands that to Litestar's OpenAPI). Examples: `HexStr` (0x string),
  `Amount` (decimal string: JS loses precision past 2**53), `Percent`
  (0–100, `Decimal`, JSON number), `Timestamp` (wraps a UTC `datetime`,
  whole seconds, RFC 3339 `…Z`). Other types needing a specific shape
  implement `HasToWire.to_wire()`.

Wire types use the wire's field names, camelCase included; there is no
renaming layer. Each camelCase field carries its own `# noqa: N815` with
the reason. Open namespaces (MCP `_meta`) are typed `dict[str, Any]` and
the entries we use are hydrated from them explicitly.

Aliases used in hydrated fields are plain unions (`Url = SecureUrl | str`),
not `type` statements: dacite cannot see through `TypeAliasType`.

## 7. Small things

- Line length 100; ruff formats. Don't hand-align against the formatter.
- Prefer a validating constructor at a boundary over trusting input and
  failing later. Parse, don't validate.
- Comments name the specific regression that returns silently if deleted.
  If you cannot name one, delete the comment.
- No lazy imports. The codebase is small; import at the top.
- `per-file-ignores` in `ruff.toml` for structural reasons only (tests
  using assert, subprocess calls in chain tools), documented in the config.
