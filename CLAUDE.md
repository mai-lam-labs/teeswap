# TEESwap — Claude Code guidelines

## Type errors and lint warnings

**Never reflexively add ignore/suppress comments to satisfy type checkers or linters.**

When a type error or lint warning appears:

1. **Understand the error.** What is the checker actually telling you? Is it a real type mismatch, a missing constraint, or a limitation in a third-party library's type stubs?
2. **Fix the root cause first.** Can the code be restructured to satisfy the checker? Is there a wrapper, a type narrowing, or a better API that avoids the issue?
3. **If suppression is genuinely needed**, explain why to the user and get explicit confirmation before adding an ignore comment. The comment must include a reason.
4. **Never batch-suppress.** Each suppression is a decision, not boilerplate.

This applies to `# noqa`, `# type: ignore`, `# pyrefly: ignore`, `# pyright: ignore`, per-file-ignores in ruff.toml, and any other mechanism that silences a checker.

The exception is per-file-ignores in `ruff.toml` for structural reasons documented in the config (e.g. tests using `assert`, subprocess calls in chain tool wrappers). Those are design decisions, not quick fixes.

## Error hierarchy

Errors are structural — each module owns its own error types inheriting from `TeeSwapError` in `common.py`. Do not create a centralised `errors.py`. When adding errors, put them in the module where they're raised.

## Types and schemas

Types are defined once as dataclasses in their owning module. Schemas (MCP, OpenAPI, JSON Schema) are derived from types via Litestar's schema generation. Do not hand-write JSON Schema or duplicate field definitions.

## Dependencies

Use dacite for dict-to-dataclass hydration. Use Litestar's native schema generation for API schemas. Do not introduce new serialisation frameworks without discussion.

## Python version

Target Python 3.14. Use modern features (PEP 649 deferred annotations, built-in generics, StrEnum).

## Toolchain

`make check` must pass: ruff lint + ruff format + ty + pyrefly strict + pytest. Do not commit code that fails any gate.
