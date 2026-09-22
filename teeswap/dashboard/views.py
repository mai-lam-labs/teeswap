"""Dashboard HTML rendering."""

import time

from ..blockchain import RpcMonitor
from ..blockchain.rpc import RpcStatus
from ..facilitator import FacilitatorMonitor

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


def _head(title: str, css: str) -> str:
    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title>"
        f"<style>{css}</style>"
        "</head><body>"
    )


def render_login(error: str = "") -> str:
    err = f'<p class="error">{error}</p>' if error else ""
    return (
        _head("Operator Login", _LOGIN_CSS)
        + '<form method="POST" action="/operator/login">'
        + f"<h1>Operator Login</h1>{err}"
        + '<label for="password">Password</label>'
        + '<input type="password" name="password" id="password" autofocus>'
        + '<button type="submit">Log in</button>'
        + "</form></body></html>"
    )


def _nav(active: str) -> str:
    items = [("Facilitators", "/operator"), ("RPCs", "/operator/rpcs")]
    links = ""
    for label, href in items:
        cls = ' class="active"' if label == active else ""
        links += f'<a href="{href}"{cls}>{label}</a>'
    return f"<nav>{links}</nav>"


def render_facilitators(monitor: FacilitatorMonitor) -> str:
    snapshot = monitor.snapshot
    rows = ""
    for url, status in sorted(snapshot.facilitators.items()):
        health = (
            '<span class="up">UP</span>' if status.healthy else '<span class="down">DOWN</span>'
        )
        networks = ", ".join(sorted({k.network for k in status.kinds})) if status.kinds else "—"
        schemes = ", ".join(sorted({k.scheme for k in status.kinds})) if status.kinds else "—"
        extensions = ", ".join(status.extensions) if status.extensions else "—"
        age = f"{time.time() - status.last_polled:.0f}s ago"
        rows += (
            f"<tr><td>{health}</td>"
            f'<td class="mono">{url}</td>'
            f"<td>{schemes}</td>"
            f'<td class="nets">{networks}</td>'
            f"<td>{extensions}</td>"
            f'<td class="age">{age}</td></tr>'
        )

    kind_count = len(snapshot.available_kinds)
    net_count = len(snapshot.available_networks)
    fac_count = len(snapshot.facilitators)
    healthy = sum(1 for f in snapshot.facilitators.values() if f.healthy)

    empty = '<tr><td colspan="6" style="color:#a8a29e">No facilitators configured</td></tr>'

    return (
        _head("Operator Dashboard", _DASH_CSS)
        + '<div class="c">'
        + '<a href="/operator/logout" class="lo">Log out</a>'
        + "<h1>Operator Dashboard</h1>"
        + '<p class="sub">TEESwap deployment health</p>'
        + _nav("Facilitators")
        + '<div class="stats">'
        + f'<div class="stat"><div class="sv">{healthy}/{fac_count}</div>'
        + '<div class="sl">Facilitators healthy</div></div>'
        + f'<div class="stat"><div class="sv">{kind_count}</div>'
        + '<div class="sl">Payment kinds</div></div>'
        + f'<div class="stat"><div class="sv">{net_count}</div>'
        + '<div class="sl">Networks</div></div>'
        + "</div>"
        + '<div class="ox"><table>'
        + "<thead><tr>"
        + "<th>Status</th><th>Facilitator</th><th>Schemes</th>"
        + "<th>Networks</th><th>Extensions</th><th>Polled</th>"
        + "</tr></thead>"
        + f"<tbody>{rows or empty}</tbody>"
        + "</table></div>"
        + "</div></body></html>"
    )


def _render_rpc_row(chain_name: str, url: str, status: RpcStatus | None) -> str:
    if status is None:
        return (
            f'<tr><td><span class="warn">PENDING</span></td>'
            f"<td>{chain_name}</td>"
            f'<td class="mono">{url}</td>'
            f"<td>—</td><td>—</td><td>—</td><td>—</td><td></td></tr>"
        )

    health = '<span class="up">UP</span>' if status.healthy else '<span class="down">DOWN</span>'

    if status.chain_id_match is None:
        id_match = "—"
    elif status.chain_id_match:
        id_match = '<span class="up">OK</span>'
    else:
        id_match = '<span class="down">MISMATCH</span>'

    block = str(status.block_height) if status.block_height is not None else "—"
    latency = (
        f'<span class="lat">{status.latency_ms:.0f}ms</span>'
        if status.latency_ms is not None
        else "—"
    )
    age = f"{time.time() - status.last_polled:.0f}s ago"
    err = status.error or ""

    return (
        f"<tr><td>{health}</td>"
        f"<td>{chain_name}</td>"
        f'<td class="mono">{url}</td>'
        f"<td>{id_match}</td>"
        f"<td>{block}</td>"
        f"<td>{latency}</td>"
        f'<td class="age">{age}</td>'
        f'<td class="mono" style="color:#a8a29e">{err}</td></tr>'
    )


def render_rpcs(monitor: RpcMonitor) -> str:
    snapshot = monitor.snapshot
    rows = ""
    total = 0
    healthy = 0
    healthy_chains: set[str] = set()

    for rpc_config in monitor.configs:
        chain = monitor.registry.lookup(rpc_config.chain)
        chain_name = chain.name if chain else rpc_config.chain
        for url in rpc_config.urls:
            total += 1
            status = snapshot.endpoints.get(url)
            if status is not None and status.healthy:
                healthy += 1
                if chain is not None:
                    healthy_chains.add(chain.caip2)
            rows += _render_rpc_row(chain_name, url, status)

    empty = '<tr><td colspan="8" style="color:#a8a29e">No RPCs configured</td></tr>'

    return (
        _head("Operator Dashboard — RPCs", _DASH_CSS)
        + '<div class="c">'
        + '<a href="/operator/logout" class="lo">Log out</a>'
        + "<h1>Operator Dashboard</h1>"
        + '<p class="sub">TEESwap deployment health</p>'
        + _nav("RPCs")
        + '<div class="stats">'
        + f'<div class="stat"><div class="sv">{healthy}/{total}</div>'
        + '<div class="sl">RPCs healthy</div></div>'
        + f'<div class="stat"><div class="sv">{len(healthy_chains)}</div>'
        + '<div class="sl">Chains reachable</div></div>'
        + "</div>"
        + '<div class="ox"><table>'
        + "<thead><tr>"
        + "<th>Status</th><th>Chain</th><th>URL</th><th>Chain ID</th>"
        + "<th>Block</th><th>Latency</th><th>Polled</th><th>Error</th>"
        + "</tr></thead>"
        + f"<tbody>{rows or empty}</tbody>"
        + "</table></div>"
        + "</div></body></html>"
    )
