import asyncio
import grp
import json
import os
import pwd
import sys
import time
from dataclasses import dataclass
from typing import Any

import click
import httpx
import uvicorn

from .app import make_http_app
from .common import DEFAULT_HOST, DEFAULT_PORT, PKG_NAME, PKG_VERSION
from .config import TeeSwapConfig
from .crypto.attestation import Signer
from .crypto.hpke import HpkeKeypair
from .crypto.vaportpm import VaportpmOutput, attest_boot, derive_pcr_bound
from .facilitator import poll_facilitator
from .instance import KeyMaterial, TeeSwap
from .mcp import MCP_PROTOCOL_VERSION, jsonl_loop
from .types import Hex32
from .wire import WireStruct, decode_object, encode


def _load_config(config_path: str | None) -> TeeSwapConfig:
    if config_path is None:
        return TeeSwapConfig()
    with open(config_path, "rb") as f:
        raw = decode_object(f.read())
    return TeeSwapConfig.from_dict(raw.get(PKG_NAME, {}))


@click.group()
@click.version_option(PKG_VERSION, prog_name=PKG_NAME)
def cli() -> None:
    """TEESwap — cross-chain swap aggregator in a verified TEE."""


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8402, type=int, help="Bind port")
@click.option(
    "--config", "config_path", default=None, type=click.Path(exists=True), help="JSON config file"
)
@click.option("--key-fd", default=None, type=int, help="File descriptor to read key material from")
def serve(host: str, port: int, config_path: str | None, key_fd: int | None) -> None:
    """Start the HTTP server (MCP + REST + x402)."""
    if key_fd is not None:
        keys, config = _read_key_material(key_fd)
    else:
        keys = None
        config = _load_config(config_path)
    instance = TeeSwap(config=config, keys=keys)
    app = make_http_app(instance)
    production = key_fd is not None
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        access_log=not production,
        log_level="warning" if production else "info",
    )


@cli.command()
@click.option(
    "--config", "config_path", default=None, type=click.Path(exists=True), help="JSON config file"
)
def stdio(config_path: str | None) -> None:
    """Run MCP over stdin/stdout (JSONL)."""
    instance = TeeSwap(config=_load_config(config_path))
    session = instance.sessions.create(MCP_PROTOCOL_VERSION)
    asyncio.run(jsonl_loop(instance.dispatcher, instance.sessions, session, sys.stdin, sys.stdout))


@cli.command(name="container-init")
def container_init() -> None:
    """Stage2 boot entrypoint — keygen, attest, drop privs, re-exec serve."""
    config_raw = sys.stdin.buffer.read()
    raw: dict[str, Any] = decode_object(config_raw) if config_raw else {}
    config = TeeSwapConfig.from_dict(raw.get(PKG_NAME, {}))

    signing_seed = derive_pcr_bound(f"{PKG_NAME}-signing-v1", 32)
    hpke_seed = derive_pcr_bound(f"{PKG_NAME}-hpke-v1", 32)
    evm_seed = derive_pcr_bound(f"{PKG_NAME}-evm-v1", 32)
    signer = Signer(private_key_bytes=signing_seed)
    hpke_keypair = HpkeKeypair.from_seed(hpke_seed)

    click.echo(f"signer:   {signer.public_key_bytes.hex()}", err=True)
    click.echo(f"hpke:     {hpke_keypair.public_key_bytes.hex()}", err=True)

    boot_attestation: VaportpmOutput | None = None
    try:
        boot_att = attest_boot(
            signer=signer,
            hpke_public_key=hpke_keypair.public_key_bytes,
            timestamp=int(time.time()),
        )
        boot_attestation = boot_att.attestation
        click.echo(f"attested: nonce={boot_att.nonce.hex()}", err=True)
    except Exception as e:  # noqa: BLE001
        click.echo(f"attestation failed: {e}", err=True)
        sys.exit(1)

    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    _write_key_material(
        write_fd,
        KeyMaterialWire(
            signing_key=Hex32.from_bytes(signer.private_key_bytes),
            hpke_private_key=Hex32.from_bytes(hpke_keypair.private_key_bytes),
            hpke_public_key=Hex32.from_bytes(hpke_keypair.public_key_bytes),
            evm_root_key=Hex32.from_bytes(evm_seed),
            config=config,
            boot_attestation=boot_attestation,
        ),
    )

    _drop_privileges("nobody")

    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            PKG_NAME,
            "serve",
            "--host",
            DEFAULT_HOST,
            "--port",
            str(DEFAULT_PORT),
            "--key-fd",
            str(read_fd),
        ],
    )


@cli.command()
def version() -> None:
    """Print version and package info."""
    click.echo(f"{PKG_NAME} {PKG_VERSION}")
    click.echo(f"Python {sys.version}")


@cli.command(name="tools")
def list_tools() -> None:
    """List registered MCP tools and their schemas."""
    instance = TeeSwap()
    for entry in instance.dispatcher.tools_list():
        click.echo(f"  {entry['name']}")
        click.echo(f"    {entry['description']}")


@cli.command(name="x402-status")
@click.argument("url")
def x402_status(url: str) -> None:
    """Poll an x402 facilitator and print its status."""

    async def _run() -> None:
        url_clean = url.rstrip("/")
        async with httpx.AsyncClient() as client:
            status = await poll_facilitator(client, url_clean)
        click.echo(f"health:  {'UP' if status.healthy else 'DOWN'}")
        if not status.healthy:
            sys.exit(1)
        result = {
            "url": status.url,
            "healthy": status.healthy,
            "kinds": [{"scheme": k.scheme, "network": k.network} for k in status.kinds],
            "extensions": list(status.extensions),
            "signers": list(status.signers),
        }
        click.echo(json.dumps(result, indent=2))

    asyncio.run(_run())


# --- Key material over pipe ---


@dataclass(frozen=True, slots=True)
class KeyMaterialWire(WireStruct):
    """Key material handed from container-init to serve over an inherited pipe."""

    signing_key: Hex32
    hpke_private_key: Hex32
    hpke_public_key: Hex32
    evm_root_key: Hex32
    config: TeeSwapConfig
    boot_attestation: VaportpmOutput | None = None


def _write_key_material(fd: int, wire: KeyMaterialWire) -> None:
    with os.fdopen(fd, "wb") as f:
        f.write(encode(wire))


def _read_key_material(fd: int) -> tuple[KeyMaterial, TeeSwapConfig]:
    with os.fdopen(fd, "rb") as f:
        wire = KeyMaterialWire.from_dict(decode_object(f.read()))

    signer = Signer(private_key_bytes=wire.signing_key.to_bytes())
    if wire.boot_attestation is not None:
        signer.set_boot_attestation(wire.boot_attestation)

    hpke_keypair = HpkeKeypair(
        private_key_bytes=wire.hpke_private_key.to_bytes(),
        public_key_bytes=wire.hpke_public_key.to_bytes(),
    )
    keys = KeyMaterial(
        signer=signer,
        hpke_keypair=hpke_keypair,
        evm_root_key=wire.evm_root_key.to_bytes(),
    )
    return keys, wire.config


# --- Privilege drop ---


def _drop_privileges(user: str) -> None:
    if os.getuid() != 0:
        click.echo(f"not root, skipping privilege drop (running as uid={os.getuid()})", err=True)
        return

    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        click.echo(f"user '{user}' not found, skipping privilege drop", err=True)
        return

    groups = [g.gr_gid for g in grp.getgrall() if user in g.gr_mem]
    os.setgroups(groups)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir
    click.echo(f"dropped privileges to {user} (uid={pw.pw_uid})", err=True)
