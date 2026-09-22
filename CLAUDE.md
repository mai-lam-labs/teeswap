# TEESwap — Claude Code guidelines

## Threat model

The machine operator is an adversary. TEESwap runs inside a TEE where the operator controls the host, the network, and the cloud metadata (user-data JSON). Code and configuration baked into the container image are measured into TPM PCRs and therefore trusted. Anything read at runtime from operator-supplied sources (stdin config, environment, network) is untrusted input.

Never let operator-supplied config control security-sensitive behaviour: privilege level, bind addresses, binary paths, key material, or anything that would change the attestation's meaning. Hardcode these. The `ContainerInitConfig` dataclass exists for per-instance operator data (e.g. profit address) that does not affect the trust boundary.

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

`make check` must pass: ruff lint + ruff format + ty + pyrefly strict + pytest. Do not commit code that fails any gate. Run `make check` before every commit — do not rely on CI to catch errors. Always use `make check`, never run ruff, ty, or pyrefly directly — the Makefile is the single source of truth for which checks run and how.

## Concurrency model

Single process, single asyncio event loop, no worker forking. All state — signing keys, facilitator snapshot, invoice log, sessions — lives in one process. Concurrency is async I/O, not multiprocessing. uvicorn runs with `workers=1` explicitly. Do not introduce threading, multiprocessing, or multi-worker configurations — they break the shared in-process state model that the TEE architecture relies on.

## Documentation

Docs in `docs/` describe design decisions and architecture at a high level. They should focus on the *why* — what tradeoffs were made, what the trust model is, how components relate. Implementation details like specific algorithms, library choices, or function signatures belong in the code (docstrings, module-level comments), not in architecture docs. If it could change without affecting the design, it's not an architectural decision.

## Self-contained project

Everything lives inside the project directory. Do not write to `/tmp`, `~/.local`, `~/`, or any path outside the working directory. Downloads, build artefacts, tool binaries, caches — all go under the project tree (e.g. `.uv/`, `dist/`, `.venv/`). The Makefile already follows this pattern: uv, ruff, Python interpreters, and the venv are all project-local. Do not break this invariant.

## External repos

Never write to files outside the teeswap working directory without explicit permission. The lockboot repo (`/home/user/Projects/lockboot`) contains critical-path TEE binaries. When the user asks for a "sketch" of changes to external code, present it as text in the conversation — do not write to disk. Never run destructive git commands (`checkout --`, `reset`, `clean`) in repos whose state you haven't verified.
