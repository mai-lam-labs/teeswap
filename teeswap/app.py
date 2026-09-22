from typing import Any

from litestar import Litestar, MediaType, Request, Response, Router, post
from litestar.openapi import OpenAPIConfig
from litestar.status_codes import HTTP_200_OK

from .blockchain import RpcMonitor
from .common import PKG_NAME, PKG_VERSION, from_dict
from .dashboard import create_dashboard_router
from .facilitator import FacilitatorMonitor
from .mcp import (
    Dispatcher,
    JsonRpcRequest,
    SessionManager,
    Tool,
    handle_mcp_request,
)


def _make_rest_handler(tool: Tool) -> Any:
    defn = tool.definition
    input_type = defn.input_type

    async def handler(data: Any) -> Response[Any]:
        result = await tool.execute(data)
        body, media_type = result.to_rest()
        return Response(content=body, status_code=HTTP_200_OK, media_type=media_type)

    short_name = defn.name.removeprefix(f"{PKG_NAME}_")
    handler.__name__ = short_name
    handler.__qualname__ = short_name
    handler.__annotations__["data"] = input_type

    return post(
        path=defn.rest_path,
        summary=short_name,
        description=defn.description,
    )(handler)


def _make_mcp_handler(dispatcher: Dispatcher, sessions: SessionManager) -> Any:
    @post(
        path="/",
        summary="MCP JSON-RPC",
        description="MCP JSON-RPC endpoint (stateful + stateless). "
        "Supports initialize, server/discover, tools/list, tools/call, and verifiable-tools/call.",
    )
    async def mcp_handler(request: Request[Any, Any, Any]) -> Response[Any]:
        body = await request.json()
        rpc = from_dict(JsonRpcRequest, body)
        method = request.headers.get("Mcp-Method", rpc.method)
        session_id = request.headers.get("Mcp-Session-Id")

        mcp_result = await handle_mcp_request(dispatcher, sessions, method, rpc, session_id)

        resp = Response(
            content=mcp_result.body,
            status_code=HTTP_200_OK,
            media_type=MediaType.JSON,
        )
        is_stateful = session_id is not None or method == "initialize"
        if is_stateful and mcp_result.session_id is not None:
            resp.headers["Mcp-Session-Id"] = mcp_result.session_id
        return resp

    return mcp_handler


def create_app(
    dispatcher: Dispatcher,
    facilitator_monitor: FacilitatorMonitor,
    rpc_monitor: RpcMonitor,
    operator_password: str,
) -> Litestar:
    sessions = SessionManager()
    rest_handlers = [
        _make_rest_handler(tool) for tool in dispatcher.tools.values() if not tool.requires_session
    ]

    api_router = Router(
        path=f"/{PKG_NAME}",
        route_handlers=rest_handlers,
        tags=[PKG_NAME],
    )

    mcp_router = Router(
        path="/mcp",
        route_handlers=[_make_mcp_handler(dispatcher, sessions)],
        tags=["MCP"],
    )

    routers = [
        api_router,
        mcp_router,
        create_dashboard_router(operator_password, facilitator_monitor, rpc_monitor),
    ]

    async def on_startup() -> None:
        facilitator_monitor.start()
        rpc_monitor.start()

    async def on_shutdown() -> None:
        facilitator_monitor.stop()
        rpc_monitor.stop()

    return Litestar(
        route_handlers=routers,
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        openapi_config=OpenAPIConfig(
            title=PKG_NAME,
            version=PKG_VERSION,
            description="Cross-chain swap aggregator in a verified TEE",
        ),
    )
