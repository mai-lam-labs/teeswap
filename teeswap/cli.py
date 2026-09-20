import asyncio
import json
import sys
from importlib.metadata import metadata
from typing import Any

import click
import uvicorn

from .app import create_app
from .attestation import Signer
from .common import from_dict
from .hpke import generate_keypair
from .mcp import (
    MCP_PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    Dispatcher,
    JsonRpcRequest,
    SessionManager,
    handle_mcp_request,
)
from .tools import register_all


def _make_dispatcher() -> Dispatcher:
    signer = Signer.from_env()
    hpke_keypair = generate_keypair()
    dispatcher = Dispatcher(signer=signer, hpke_keypair=hpke_keypair)
    register_all(dispatcher)
    return dispatcher


@click.group()
@click.version_option(SERVER_VERSION, prog_name=SERVER_NAME)
def cli() -> None:
    """TEESwap — cross-chain swap aggregator in a verified TEE."""


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8402, type=int, help="Bind port")
def serve(host: str, port: int) -> None:
    """Start the HTTP server (MCP + REST + x402)."""
    dispatcher = _make_dispatcher()
    app = create_app(dispatcher)
    uvicorn.run(app, host=host, port=port)


@cli.command()
def stdio() -> None:
    """Run MCP over stdin/stdout (JSONL)."""
    dispatcher = _make_dispatcher()
    asyncio.run(_stdio_loop(dispatcher))


@cli.command()
def version() -> None:
    """Print version and package info."""
    meta = metadata(SERVER_NAME)
    click.echo(f"{meta['Name']} {meta['Version']}")
    click.echo(f"Python {sys.version}")


@cli.command(name="tools")
def list_tools() -> None:
    """List registered MCP tools and their schemas."""
    dispatcher = _make_dispatcher()
    for entry in dispatcher.tools_list():
        click.echo(f"  {entry['name']}")
        click.echo(f"    {entry['description']}")


async def _stdio_loop(dispatcher: Dispatcher) -> None:
    sessions = SessionManager()
    stdio_session = sessions.create(MCP_PROTOCOL_VERSION)

    for raw_line in sys.stdin:
        stripped = raw_line.strip()
        if not stripped:
            continue

        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as e:
            _write_error(None, -32700, f"parse error: {e}")
            continue

        try:
            rpc = from_dict(JsonRpcRequest, raw)
        except (TypeError, KeyError, ValueError) as e:
            _write_error(raw.get("id"), -32600, f"invalid request: {e}")
            continue

        result = await handle_mcp_request(
            dispatcher=dispatcher,
            sessions=sessions,
            method=rpc.method,
            rpc=rpc,
            session_id=stdio_session.session_id,
        )

        _write(result.body)


def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _write_error(req_id: Any, code: int, message: str) -> None:
    _write(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }
    )
