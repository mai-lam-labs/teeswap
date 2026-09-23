"""Dashboard HTML rendering."""

import time

from ..blockchain.rpc import RpcMonitor, RpcStatus
from ..execution.invoice import InvoiceRegistry
from ..facilitator import FacilitatorMonitor
from .html import (
    a,
    button,
    div,
    document,
    form,
    h1,
    input_,
    label,
    meta,
    nav,
    p,
    span,
    style,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
)

_LOGIN_CSS = """
body { font-family: system-ui, sans-serif; background: #0c0a09; color: #e7e5e4;
       display: flex; justify-content: center; align-items: center;
       min-height: 100vh; margin: 0; }
form { background: #1c1917; padding: 32px; border-radius: 8px;
       border: 1px solid #292524; width: 100%; max-width: 320px; }
h1 { font-size: 1.125rem; margin: 0 0 20px; }
label { font-size: 0.8125rem; color: #a8a29e; display: block; margin-bottom: 4px; }
input { width: 100%; padding: 8px 12px; border: 1px solid #292524;
        border-radius: 4px; background: #0c0a09; color: #e7e5e4;
        font-size: 0.875rem; box-sizing: border-box; }
button { width: 100%; padding: 10px; margin-top: 16px; border: none;
         border-radius: 4px; background: #e7e5e4; color: #0c0a09;
         font-weight: 500; cursor: pointer; font-size: 0.875rem; }
.error { color: #fca5a5; font-size: 0.8125rem; margin: 0 0 12px; }
"""

_DASH_CSS = """
body { font-family: system-ui, sans-serif; background: #0c0a09;
       color: #e7e5e4; margin: 0; padding: 24px 16px; }
.c { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 1rem; font-weight: 600; margin: 24px 0 12px; }
.sub { color: #a8a29e; font-size: 0.8125rem; margin: 0 0 24px; }
nav { margin-bottom: 24px; border-bottom: 1px solid #292524;
      padding-bottom: 12px; }
nav a { color: #a8a29e; text-decoration: none; margin-right: 16px;
        font-size: 0.8125rem; }
nav a.active { color: #e7e5e4; }
.stats { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
.stat { background: #1c1917; border: 1px solid #292524;
        border-radius: 6px; padding: 12px 16px; }
.sv { font-size: 1.5rem; font-weight: 600;
      font-variant-numeric: tabular-nums; }
.sl { font-size: 0.6875rem; text-transform: uppercase;
      letter-spacing: 0.05em; color: #a8a29e; margin-top: 2px; }
table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
th { text-align: left; font-weight: 500; font-size: 0.6875rem;
     text-transform: uppercase; letter-spacing: 0.05em; color: #a8a29e;
     padding: 8px 8px 8px 0; border-bottom: 1px solid #292524; }
td { padding: 10px 8px 10px 0; border-bottom: 1px solid #1c1917;
     vertical-align: top; }
.mono { font-family: ui-monospace, monospace; font-size: 0.75rem;
        word-break: break-all; }
.nets { font-family: ui-monospace, monospace; font-size: 0.6875rem;
        max-width: 300px; }
.age { color: #a8a29e; font-variant-numeric: tabular-nums; }
.up { color: #4ade80; }
.down { color: #fca5a5; }
.warn { color: #fbbf24; }
.ox { overflow-x: auto; }
.lo { float: right; color: #a8a29e; text-decoration: none;
      font-size: 0.8125rem; }
.lat { font-variant-numeric: tabular-nums; }
"""

_NAV_ITEMS = [
    ("Invoices", "/operator/invoices"),
    ("Processes", "/operator/processes"),
    ("Facilitators", "/operator/facilitators"),
    ("RPCs", "/operator/rpcs"),
]


def _badge(label_text: str, css: str) -> None:
    span(label_text, cls=css)


def _stat(value: str, label_text: str) -> None:
    with div(cls="stat"):
        div(value, cls="sv")
        div(label_text, cls="sl")


def _nav_bar(active: str) -> None:
    with nav():
        for label_text, href in _NAV_ITEMS:
            cls = "active" if label_text == active else False
            a(label_text, href=href, cls=cls)


def _dash_page(title: str) -> document:
    doc = document(title)
    with doc.head:
        meta(charset="utf-8")
        meta(name="viewport", content="width=device-width,initial-scale=1")
        style(_DASH_CSS)
    return doc


def _dash_header(active_nav: str) -> None:
    a("Log out", href="/operator/logout", cls="lo")
    h1("Operator Dashboard")
    p("TEESwap deployment health", cls="sub")
    _nav_bar(active_nav)


def _header_cols(*cols: str) -> None:
    with thead(), tr():
        for col in cols:
            th(col)


def _empty_row(message: str, colspan: int) -> None:
    with tr():
        td(message, colspan=str(colspan), style="color:#a8a29e")


# --- Login ---


def render_login(error: str = "") -> str:
    doc = document("Operator Login")
    with doc.head:
        meta(charset="utf-8")
        meta(name="viewport", content="width=device-width,initial-scale=1")
        style(_LOGIN_CSS)
    with doc.body, form(method="POST", action="/operator/login"):
        h1("Operator Login")
        if error:
            p(error, cls="error")
        label("Password", for_="password")
        input_(type="password", name="password", id="password", autofocus=True)
        button("Log in", type="submit")
    return str(doc)


# --- Facilitators ---


def render_facilitators(monitor: FacilitatorMonitor) -> str:
    snapshot = monitor.snapshot
    fac_count = len(snapshot.facilitators)
    healthy_count = sum(1 for f in snapshot.facilitators.values() if f.healthy)

    doc = _dash_page("Operator Dashboard — Facilitators")
    with doc.body, div(cls="c"):
        _dash_header("Facilitators")
        with div(cls="stats"):
            _stat(f"{healthy_count}/{fac_count}", "Facilitators healthy")
            _stat(str(len(snapshot.available_kinds)), "Payment kinds")
            _stat(str(len(snapshot.available_networks)), "Networks")

        with div(cls="ox"), table():
            _header_cols("Status", "Facilitator", "Schemes", "Networks", "Extensions", "Polled")
            with tbody():
                if not snapshot.facilitators:
                    _empty_row("No facilitators configured", 6)
                else:
                    for url, status in sorted(snapshot.facilitators.items()):
                        with tr():
                            with td():
                                _badge(
                                    "UP" if status.healthy else "DOWN",
                                    "up" if status.healthy else "down",
                                )
                            td(url, cls="mono")
                            td(", ".join(sorted({k.scheme for k in status.kinds})) or "—")
                            td(
                                ", ".join(sorted({k.network for k in status.kinds})) or "—",
                                cls="nets",
                            )
                            td(", ".join(status.extensions) or "—")
                            td(f"{time.time() - status.last_polled:.0f}s ago", cls="age")
    return str(doc)


# --- Invoices ---


def render_invoices(registry: InvoiceRegistry) -> str:
    invoices = registry.all()
    total = len(invoices)
    active_count = sum(1 for i in invoices if i.is_active)

    doc = _dash_page("Operator Dashboard — Invoices")
    with doc.body, div(cls="c"):
        _dash_header("Invoices")
        with div(cls="stats"):
            _stat(str(total), "Total invoices")
            _stat(str(active_count), "Active")

        with div(cls="ox"), table():
            _header_cols(
                "Status", "ID", "Chain", "Have", "Want", "Deposit", "Actions", "Current", "Created"
            )
            with tbody():
                if not invoices:
                    _empty_row("No invoices", 9)
                else:
                    for inv in sorted(invoices, key=lambda i: i.created_at, reverse=True):
                        css = (
                            "up"
                            if inv.is_active
                            else ("down" if inv.status.value == "failed" else "warn")
                        )
                        current = inv.current_action
                        with tr():
                            with td():
                                _badge(inv.status.value.upper(), css)
                            td(str(inv.id), cls="mono")
                            td(", ".join(sorted({i.token.chain.name for i in inv.quote.inputs})))
                            td(", ".join(f"{i.amount} {i.token.symbol}" for i in inv.quote.inputs))
                            td(f"{len(inv.request.outputs)} output(s)")
                            deposits = [d.address.value[:10] + "…" for d in inv.deposits]
                            td(", ".join(deposits) or "—", cls="mono")
                            td(str(len(inv.actions)))
                            td(current.description if current else "—")
                            td(inv.created_at.dt.strftime("%Y-%m-%d %H:%M"), cls="age")
    return str(doc)


# --- Processes ---


def render_processes(registry: InvoiceRegistry) -> str:
    active = registry.active()

    doc = _dash_page("Operator Dashboard — Processes")
    with doc.body, div(cls="c"):
        _dash_header("Processes")
        with div(cls="stats"):
            _stat(str(len(active)), "Active processes")

        with div(cls="ox"), table():
            _header_cols("Invoice", "Action", "Step", "Status", "Chain", "Started")
            with tbody():
                if not active:
                    _empty_row("No active processes", 6)
                else:
                    for inv in active:
                        action = inv.current_action
                        step = action.current_step if action else None
                        with tr():
                            td(str(inv.id), cls="mono")
                            td(action.description if action else "—")
                            td(step.operation if step else "—")
                            with td():
                                _badge(step.status.value if step else "—", "warn")
                            td(step.chain.name if step and step.chain else "—")
                            td(
                                step.started_at.dt.strftime("%H:%M:%S")
                                if step and step.started_at
                                else "—",
                                cls="age",
                            )
    return str(doc)


# --- RPCs ---


def _rpc_row(chain_name: str, url: str, status: RpcStatus | None) -> None:
    with tr():
        if status is None:
            with td():
                _badge("PENDING", "warn")
            td(chain_name)
            td(url, cls="mono")
            for _ in range(5):
                td("—")
            return

        with td():
            _badge("UP" if status.healthy else "DOWN", "up" if status.healthy else "down")
        td(chain_name)
        td(url, cls="mono")

        if status.chain_id_match is None:
            td("—")
        elif status.chain_id_match:
            with td():
                _badge("OK", "up")
        else:
            with td():
                _badge("MISMATCH", "down")

        td(str(status.block_height) if status.block_height is not None else "—")
        if status.latency_ms is not None:
            with td():
                span(f"{status.latency_ms:.0f}ms", cls="lat")
        else:
            td("—")
        td(f"{time.time() - status.last_polled:.0f}s ago", cls="age")
        td(status.error or "", cls="mono", style="color:#a8a29e")


def render_rpcs(monitor: RpcMonitor) -> str:
    snapshot = monitor.snapshot
    total = 0
    healthy = 0
    healthy_chains: set[str] = set()

    for rpc_config in monitor.configs:
        chain = monitor.registry.lookup(rpc_config.chain)
        for url in rpc_config.urls:
            total += 1
            status = snapshot.endpoints.get(str(url))
            if status is not None and status.healthy:
                healthy += 1
                if chain is not None:
                    healthy_chains.add(chain.caip2)

    doc = _dash_page("Operator Dashboard — RPCs")
    with doc.body, div(cls="c"):
        _dash_header("RPCs")
        with div(cls="stats"):
            _stat(f"{healthy}/{total}", "RPCs healthy")
            _stat(str(len(healthy_chains)), "Chains reachable")

        with div(cls="ox"), table():
            _header_cols(
                "Status", "Chain", "URL", "Chain ID", "Block", "Latency", "Polled", "Error"
            )
            with tbody():
                if total == 0:
                    _empty_row("No RPCs configured", 8)
                else:
                    for rpc_config in monitor.configs:
                        chain = monitor.registry.lookup(rpc_config.chain)
                        chain_name = chain.name if chain else rpc_config.chain
                        for url in rpc_config.urls:
                            _rpc_row(chain_name, str(url), snapshot.endpoints.get(str(url)))
    return str(doc)
