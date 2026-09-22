"""Dashboard Litestar route handlers."""

import secrets

from litestar import MediaType, Request, Response, Router, get, post
from litestar.datastructures.state import State

from ..blockchain.rpc import RpcMonitor
from ..facilitator import FacilitatorMonitor
from ..invoice import InvoiceRegistry
from .auth import SESSION_COOKIE, OperatorSessions
from .views import (
    render_facilitators,
    render_invoices,
    render_login,
    render_processes,
    render_rpcs,
)


def create_dashboard_router(
    password: str,
    facilitator_monitor: FacilitatorMonitor,
    rpc_monitor: RpcMonitor,
    invoice_registry: InvoiceRegistry,
) -> Router:
    sessions = OperatorSessions()

    def _require_auth(request: Request[None, None, State]) -> bool:
        token = request.cookies.get(SESSION_COOKIE, "")
        return sessions.valid(token)

    def _redirect_login() -> Response[str]:
        return Response(
            content="",
            status_code=303,
            headers={"Location": "/operator/login"},
            media_type=MediaType.HTML,
        )

    @get("/login", media_type=MediaType.HTML)
    async def login_page() -> str:
        return render_login()

    @post("/login", media_type=MediaType.HTML)
    async def login_submit(request: Request[None, None, State]) -> Response[str]:
        form = await request.form()
        submitted = form.get("password", "")
        if not secrets.compare_digest(str(submitted), password):
            return Response(
                render_login(error="Invalid password"),
                status_code=401,
                media_type=MediaType.HTML,
            )
        token = sessions.create()
        response = Response(
            content="",
            status_code=303,
            headers={"Location": "/operator/invoices"},
            media_type=MediaType.HTML,
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            path="/operator",
        )
        return response

    @get("/logout", media_type=MediaType.HTML)
    async def logout(request: Request[None, None, State]) -> Response[str]:
        token = request.cookies.get(SESSION_COOKIE, "")
        sessions.remove(token)
        response = _redirect_login()
        response.delete_cookie(SESSION_COOKIE, path="/operator")
        return response

    @get("/", media_type=MediaType.HTML)
    async def dashboard_root(request: Request[None, None, State]) -> Response[str]:
        if not _require_auth(request):
            return _redirect_login()
        return Response(
            content="",
            status_code=303,
            headers={"Location": "/operator/invoices"},
            media_type=MediaType.HTML,
        )

    @get("/invoices", media_type=MediaType.HTML)
    async def invoices(request: Request[None, None, State]) -> Response[str]:
        if not _require_auth(request):
            return _redirect_login()
        return Response(render_invoices(invoice_registry), media_type=MediaType.HTML)

    @get("/processes", media_type=MediaType.HTML)
    async def processes(request: Request[None, None, State]) -> Response[str]:
        if not _require_auth(request):
            return _redirect_login()
        return Response(render_processes(invoice_registry), media_type=MediaType.HTML)

    @get("/facilitators", media_type=MediaType.HTML)
    async def facilitators(request: Request[None, None, State]) -> Response[str]:
        if not _require_auth(request):
            return _redirect_login()
        return Response(render_facilitators(facilitator_monitor), media_type=MediaType.HTML)

    @get("/rpcs", media_type=MediaType.HTML)
    async def rpcs(request: Request[None, None, State]) -> Response[str]:
        if not _require_auth(request):
            return _redirect_login()
        return Response(render_rpcs(rpc_monitor), media_type=MediaType.HTML)

    return Router(
        path="/operator",
        route_handlers=[
            login_page,
            login_submit,
            logout,
            dashboard_root,
            invoices,
            processes,
            facilitators,
            rpcs,
        ],
    )
