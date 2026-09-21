import asyncio
import grp
import json
import os
import pwd
import sys
import time
from dataclasses import asdict
from typing import Any

import click
import uvicorn

from .app import create_app
from .attestation import Signer
from .common import PKG_NAME, PKG_VERSION, from_dict
from .hpke import HpkeKeypair
from .mcp import (
    MCP_PROTOCOL_VERSION,
    Dispatcher,
    SessionManager,
    jsonl_loop,
)
from .tools import register_all
from .vaportpm import VaportpmOutput, attest_boot, derive_pcr_bound


def _make_dispatcher(
    signer: Signer | None = None,
    hpke_keypair: HpkeKeypair | None = None,
) -> Dispatcher:
    if signer is None:
        signer = Signer.from_env()
    if hpke_keypair is None:
        hpke_keypair = HpkeKeypair.random()
    dispatcher = Dispatcher(signer=signer, hpke_keypair=hpke_keypair)
    register_all(dispatcher)
    return dispatcher


@click.group()
@click.version_option(PKG_VERSION, prog_name=PKG_NAME)
def cli() -> None:
    """TEESwap — cross-chain swap aggregator in a verified TEE."""


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8402, type=int, help="Bind port")
@click.option("--key-fd", default=None, type=int, help="File descriptor to read key material from")
def serve(host: str, port: int, key_fd: int | None) -> None:
    """Start the HTTP server (MCP + REST + x402)."""
    signer: Signer | None = None
    hpke_keypair: HpkeKeypair | None = None

    if key_fd is not None:
        signer, hpke_keypair = _read_key_material(key_fd)

    dispatcher = _make_dispatcher(signer=signer, hpke_keypair=hpke_keypair)
    app = create_app(dispatcher)
    uvicorn.run(app, host=host, port=port)


@cli.command()
def stdio() -> None:
    """Run MCP over stdin/stdout (JSONL)."""
    dispatcher = _make_dispatcher()
    sessions = SessionManager()
    session = sessions.create(MCP_PROTOCOL_VERSION)
    asyncio.run(jsonl_loop(dispatcher, sessions, session, sys.stdin, sys.stdout))


@cli.command(name="container-init")
def container_init() -> None:
    """Stage2 boot entrypoint — keygen, attest, drop privs, re-exec serve."""
    config_raw = sys.stdin.buffer.read()
    config: dict[str, Any] = json.loads(config_raw) if config_raw else {}

    teeswap_config: dict[str, Any] = config.get("teeswap", {})
    host = teeswap_config.get("bind_host", "0.0.0.0")  # noqa: S104
    port = teeswap_config.get("bind_port", 8402)
    run_user = teeswap_config.get("run_user", "nobody")
    signing_seed = derive_pcr_bound(f"{PKG_NAME}-signing-v1", 32)
    hpke_seed = derive_pcr_bound(f"{PKG_NAME}-hpke-v1", 32)
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
    _write_key_material(write_fd, signer, hpke_keypair, boot_attestation)

    _drop_privileges(run_user)

    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            PKG_NAME,
            "serve",
            "--host",
            host,
            "--port",
            str(port),
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
    dispatcher = _make_dispatcher()
    for entry in dispatcher.tools_list():
        click.echo(f"  {entry['name']}")
        click.echo(f"    {entry['description']}")


# --- Key material over pipe ---


def _write_key_material(
    fd: int,
    signer: Signer,
    hpke_keypair: HpkeKeypair,
    boot_attestation: VaportpmOutput | None = None,
) -> None:
    obj: dict[str, Any] = {
        "signing_key": signer.private_key_bytes.hex(),
        "hpke_private_key": hpke_keypair.private_key_bytes.hex(),
        "hpke_public_key": hpke_keypair.public_key_bytes.hex(),
    }
    if boot_attestation is not None:
        obj["boot_attestation"] = asdict(boot_attestation)
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(obj).encode())


def _read_key_material(fd: int) -> tuple[Signer, HpkeKeypair]:
    with os.fdopen(fd, "rb") as f:
        raw = f.read()
    data: dict[str, Any] = json.loads(raw)
    signer = Signer(private_key_bytes=bytes.fromhex(data["signing_key"]))

    attestation_raw: dict[str, Any] | None = data.get("boot_attestation")
    if attestation_raw is not None:
        attestation = from_dict(VaportpmOutput, attestation_raw)
        signer.set_boot_attestation(attestation)

    hpke_keypair = HpkeKeypair(
        private_key_bytes=bytes.fromhex(data["hpke_private_key"]),
        public_key_bytes=bytes.fromhex(data["hpke_public_key"]),
    )
    return signer, hpke_keypair


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
