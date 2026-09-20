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
- `type` statement for type aliases.
- `StrEnum` for wire values. `@override` on every ABC override.
- `Annotated[T, Parameter(...)]` for Litestar field descriptions.
- `from __future__ import annotations` is unnecessary on 3.14. Do not add.

## 2. Types are strict and honest

The type checker is a design tool, not a formality.

1. No `Any` except at a genuinely dynamic boundary (JSON-RPC params,
   wire format dicts). Bare `dict` is implicit `Any` — pyrefly strict
   rejects it; always specify `dict[str, X]`.
2. The wire-to-typed boundary uses `from_dict[T]` (typed dacite wrapper
   in `common.py`) or explicit construction from TypedDicts — never raw
   dict access with string keys scattered through business logic.
3. Never add type ignore/suppress comments without understanding the
   error and getting explicit confirmation. Each suppression is a
   decision, not boilerplate.

### ABCs for polymorphic interfaces

`Tool`, `ToolResponse`, `Protocol` — the ABC defines the interface,
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
common.py           TeeSwapError
attestation.py        AttestationError
                        VaportpmError
                        VaportpmParseError
                        CommitmentMismatchError
chain.py              ChainError
                        RpcError / TransactionError / SigningError
state.py              OrderError
                        InvalidTransitionError / OrderExpiredError
protocol.py           RouteError
                        NoRouteError / SlippageError / PriceCapError
x402.py               PaymentError
                        InvoiceNotFoundError
```

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

dacite hydrates dicts into dataclasses via `from_dict[T]` in `common.py`
— one typed wrapper with one suppression for dacite's upstream type bug.

camelCase ↔ snake_case conversion happens at wire format boundaries
only (e.g. `verify_meta` parsing `_meta` dicts), not via automatic
renaming.

## 7. Small things

- Line length 100; ruff formats. Don't hand-align against the formatter.
- Prefer a validating constructor at a boundary over trusting input and
  failing later. Parse, don't validate.
- Comments name the specific regression that returns silently if deleted.
  If you cannot name one, delete the comment.
- No lazy imports. The codebase is small; import at the top.
- `per-file-ignores` in `ruff.toml` for structural reasons only (tests
  using assert, subprocess calls in chain tools), documented in the config.
