"""End-user invoice view — HTML and JSON representations.

No authentication: the invoice ID is the secret. Knowing it means you
are allowed to see it.

Routes:
    GET /invoice/{id}.html  — rendered work log with print-friendly CSS
    GET /invoice/{id}.json  — machine-readable status
"""

from typing import Annotated

from litestar import MediaType, Response, Router, get
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK, HTTP_404_NOT_FOUND

from ..execution.invoice import InvoiceId, InvoiceNotFoundError, InvoiceRegistry, InvoiceView
from ..types import Timestamp
from .html import (
    a,
    div,
    document,
    h1,
    h2,
    h3,
    meta,
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

_CSS = """
:root {
    --bg: #fafaf9; --fg: #1c1917; --muted: #78716c; --border: #d6d3d1;
    --surface: #ffffff; --accent: #2563eb; --success: #16a34a;
    --warning: #d97706; --danger: #dc2626; --code-bg: #f5f5f4;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
    --bg: #0c0a09; --fg: #e7e5e4; --muted: #a8a29e; --border: #292524;
    --surface: #1c1917; --accent: #60a5fa; --success: #4ade80;
    --warning: #fbbf24; --danger: #fca5a5; --code-bg: #1c1917;
}}
:root[data-theme="dark"] {
    --bg: #0c0a09; --fg: #e7e5e4; --muted: #a8a29e; --border: #292524;
    --surface: #1c1917; --accent: #60a5fa; --success: #4ade80;
    --warning: #fbbf24; --danger: #fca5a5; --code-bg: #1c1917;
}
body {
    font-family: system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--fg);
    margin: 0; padding: 32px 16px;
    line-height: 1.5;
}
.c { max-width: 720px; margin: 0 auto; }
h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 1rem; font-weight: 600; margin: 24px 0 8px; color: var(--fg); }
h3 { font-size: 0.875rem; font-weight: 600; margin: 16px 0 6px; color: var(--muted); }
p { margin: 0 0 8px; font-size: 0.875rem; }
.sub { color: var(--muted); font-size: 0.8125rem; margin: 0 0 24px; }
.operator { font-size: 0.9375rem; font-weight: 500; color: var(--muted); margin: 0 0 2px; }
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 0.6875rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.04em;
}
.badge-success { background: color-mix(in srgb, var(--success) 15%, transparent); color: var(--success); }
.badge-warning { background: color-mix(in srgb, var(--warning) 15%, transparent); color: var(--warning); }
.badge-danger { background: color-mix(in srgb, var(--danger) 15%, transparent); color: var(--danger); }
.badge-muted { background: color-mix(in srgb, var(--muted) 15%, transparent); color: var(--muted); }
.badge-active { background: color-mix(in srgb, var(--accent) 15%, transparent); color: var(--accent); }
.card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-bottom: 16px;
}
.kv { display: grid; grid-template-columns: auto 1fr; gap: 4px 16px;
      font-size: 0.8125rem; }
.kv-label { color: var(--muted); white-space: nowrap; }
.kv-value { word-break: break-all; }
.mono { font-family: ui-monospace, monospace; font-size: 0.75rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
th { text-align: left; font-weight: 500; font-size: 0.6875rem;
     text-transform: uppercase; letter-spacing: 0.05em;
     color: var(--muted); padding: 6px 8px 6px 0;
     border-bottom: 1px solid var(--border); }
td { padding: 8px 8px 8px 0; border-bottom: 1px solid var(--border);
     vertical-align: top; }
.out-main td { border-bottom: none; padding-bottom: 2px; }
.out-sub td { padding-top: 0; word-break: break-all; }
.timeline { border-left: 2px solid var(--border); margin-left: 8px; padding-left: 16px; }
.step { margin-bottom: 12px; position: relative; }
.step::before {
    content: ""; position: absolute; left: -21px; top: 6px;
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--border); border: 2px solid var(--bg);
}
.step.completed::before { background: var(--success); }
.step.executing::before { background: var(--accent); }
.step.failed::before { background: var(--danger); }
.step-header { font-size: 0.8125rem; font-weight: 500; }
.step-meta { font-size: 0.75rem; color: var(--muted); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.json-link { float: right; font-size: 0.75rem; color: var(--muted); }
.footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border);
          font-size: 0.75rem; color: var(--muted); white-space: pre-line; }
.footer p { margin: 0 0 4px; }
@media print {
    body { background: white; color: black; padding: 0; }
    .card { border: 1px solid #ccc; box-shadow: none; }
    .json-link { display: none; }
    .step::before { border-color: white; }
}
"""

_STATUS_BADGE: dict[str, str] = {
    "quoted": "badge-muted",
    "awaiting_deposit": "badge-warning",
    "pending": "badge-muted",
    "submitted": "badge-active",
    "executing": "badge-active",
    "delivered": "badge-success",
    "failed": "badge-danger",
    "expired": "badge-muted",
}


def _fmt_amount(amount: int, symbol: str, decimals: int) -> str:
    if decimals == 0:
        return f"{amount} {symbol}"
    whole = amount // (10**decimals)
    frac = amount % (10**decimals)
    frac_str = str(frac).zfill(decimals).rstrip("0") or "0"
    return f"{whole}.{frac_str} {symbol}"


def _fmt_token(symbol: str, contract: str | None, chain_caip2: str) -> str:
    if contract:
        return f"{symbol} ({contract}) on {chain_caip2}"
    return f"{symbol} (native) on {chain_caip2}"


def _fmt_time(ts: Timestamp | None) -> str:
    if ts is None:
        return "—"
    return ts.dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _elapsed(start: Timestamp | None, end: Timestamp | None) -> str:
    if start is None:
        return ""
    finish = end or Timestamp.now()
    secs = (finish - start).total_seconds()
    if secs < 60:
        return f"{secs:.0f}s"
    return f"{secs / 60:.1f}m"


def render_invoice_html(invoice: InvoiceView) -> str:
    badge_cls = _STATUS_BADGE.get(invoice.status.value, "badge-muted")
    short_id = invoice.quote_id[-8:]

    doc = document(f"Invoice {short_id}")
    with doc.head:
        meta(charset="utf-8")
        meta(name="viewport", content="width=device-width,initial-scale=1")
        style(_CSS)

    with doc.body, div(cls="c"):
        a(f"{invoice.quote_id}.json", href=f"/invoice/{invoice.quote_id}.json", cls="json-link")
        op = invoice.operator
        if op.legal_name:
            p(op.legal_name, cls="operator")
        h1(f"Invoice {invoice.quote_id}")
        p(f"Created {_fmt_time(invoice.created_at)}", cls="sub")

        # --- Status ---
        with div(cls="card"), div(cls="kv"):
            span("Status", cls="kv-label")
            with div(cls="kv-value"):
                span(invoice.status.value.replace("_", " ").upper(), cls=f"badge {badge_cls}")

            span("Expires", cls="kv-label")
            span(_fmt_time(invoice.expires_at), cls="kv-value")

        # --- Inputs ---
        if invoice.inputs:
            h2("Inputs")
            with div(cls="card"), table():
                with thead(), tr():
                    th("Deposit to")
                    th("Token")
                    th("Required")
                    th("Received")
                    th("Status")
                with tbody():
                    for inp in invoice.inputs:
                        tok = inp.deposit.amount.token
                        with tr():
                            td(inp.deposit.address.value, cls="mono")
                            td(_fmt_token(tok.symbol, tok.contract, tok.chain.caip2))
                            td(_fmt_amount(inp.deposit.amount.amount, tok.symbol, tok.decimals))
                            td(_fmt_amount(inp.received.amount, tok.symbol, tok.decimals))
                            td(inp.status.value)

        # --- Outputs ---
        if invoice.outputs:
            h2("Outputs")
            with div(cls="card"), table():
                with thead(), tr():
                    th("Recipient")
                    th("Token")
                    th("Amount")
                with tbody():
                    for out in invoice.outputs:
                        tok = out.balance.amount.token
                        with tr(cls="out-main"):
                            td(out.balance.address.value, cls="mono")
                            td(_fmt_token(tok.symbol, tok.contract, tok.chain.caip2))
                            td(_fmt_amount(out.balance.amount.amount, tok.symbol, tok.decimals))
                        with tr(cls="out-sub"), td(colspan="3"):
                            span(
                                out.status.value.upper(),
                                cls=f"badge {_STATUS_BADGE.get(out.status.value, 'badge-muted')}",
                            )
                            for tx in out.transactions:
                                span(f" {tx.hash}", cls="mono")

        # --- Custody: where every unit of the funds is now ---
        if invoice.holdings:
            h2("Custody")
            with div(cls="card"), table():
                with thead(), tr():
                    th("Custody")
                    th("Where")
                    th("Amount")
                with tbody():
                    for position in invoice.holdings:
                        tok = position.amount.token
                        with tr():
                            td(position.place.custody.value.replace("_", " "))
                            td(position.place.address.value, cls="mono")
                            td(_fmt_amount(position.amount.amount, tok.symbol, tok.decimals))

        # --- Work log ---
        if invoice.actions:
            h2("Work Log")
            for action in invoice.actions:
                chain_label = action.source_chain.caip2
                if action.destination_chain:
                    chain_label += f" → {action.destination_chain.caip2}"
                h3(f"{action.description} — {chain_label}")

                with div(cls="timeline"):
                    for step in action.steps:
                        with div(cls=f"step {step.status.value}"):
                            with div(cls="step-header"):
                                span(step.operation)
                                span(
                                    f" — {step.status.value}",
                                    cls=f"badge {_STATUS_BADGE.get(step.status.value, 'badge-muted')}",
                                    style="margin-left:8px",
                                )

                            with div(cls="step-meta"):
                                parts: list[str] = []
                                if step.chain:
                                    parts.append(step.chain.caip2)
                                if step.started_at:
                                    parts.append(f"started {_fmt_time(step.started_at)}")
                                if step.completed_at:
                                    parts.append(_elapsed(step.started_at, step.completed_at))
                                if parts:
                                    span(" · ".join(parts))

                            if step.error:
                                p(
                                    step.error,
                                    style="color:var(--danger);font-size:0.75rem;margin:4px 0 0",
                                )

                            if step.transactions:
                                with table():
                                    with thead(), tr():
                                        th("Chain")
                                        th("Transaction")
                                        th("Time")
                                    with tbody():
                                        for tx in step.transactions:
                                            with tr():
                                                td(f"{tx.chain.name} ({tx.chain.caip2})")
                                                td(tx.hash, cls="mono")
                                                td(_fmt_time(tx.timestamp))

        # --- Quote estimates ---
        h2("Quote Estimates")
        with div(cls="card"):
            with div(cls="kv"):
                gas = invoice.gas
                span("Gas estimate", cls="kv-label")
                span(
                    _fmt_amount(gas.amount, gas.token.symbol, gas.token.decimals),
                    cls="kv-value",
                )

            h3("Estimated outputs")
            with table():
                with thead(), tr():
                    th("Recipient")
                    th("Token")
                    th("Amount")
                with tbody():
                    for out in invoice.outputs:
                        est = out.balance
                        tok = est.amount.token
                        with tr():
                            td(est.address.value, cls="mono")
                            td(_fmt_token(tok.symbol, tok.contract, tok.chain.caip2))
                            td(_fmt_amount(est.amount.amount, tok.symbol, tok.decimals))

        # --- Issuer ---
        issuer = [op.legal_name, op.registration_number, op.postal_address, op.extra]
        if any(issuer):
            with div(cls="footer"):
                for value in issuer:
                    if value:
                        p(value)

    return str(doc)


def create_invoice_router(registry: InvoiceRegistry) -> Router:
    @get("/{full_id:str}", media_type=MediaType.HTML)
    async def invoice_view(
        full_id: Annotated[str, Parameter(description="Invoice ID with .html or .json extension")],
    ) -> Response[bytes]:
        quote_id, dot, fmt = full_id.rpartition(".")
        if not dot or fmt not in ("html", "json"):
            return Response(
                content=b"Use .html or .json extension",
                status_code=HTTP_404_NOT_FOUND,
                media_type=MediaType.TEXT,
            )
        try:
            view = registry.get(InvoiceId(quote_id)).view()
        except InvoiceNotFoundError:
            return Response(
                content=b"Invoice not found",
                status_code=HTTP_404_NOT_FOUND,
                media_type=MediaType.TEXT,
            )

        if fmt == "json":
            body, media_type = view.to_rest()
            return Response(content=body, status_code=HTTP_200_OK, media_type=media_type)
        return Response(
            content=render_invoice_html(view).encode(),
            status_code=HTTP_200_OK,
            media_type=MediaType.HTML,
        )

    return Router(
        path="/invoice",
        route_handlers=[invoice_view],
    )
