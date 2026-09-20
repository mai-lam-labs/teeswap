from typing import Any

from litestar import Litestar, MediaType, Request, Response, Router, post
from litestar.openapi import OpenAPIConfig
from litestar.status_codes import HTTP_200_OK

from .common import from_dict
from .mcp import (
    SERVER_NAME,
    SERVER_VERSION,
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

    short_name = defn.name.removeprefix(f"{SERVER_NAME}_")
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


def create_app(dispatcher: Dispatcher) -> Litestar:
    sessions = SessionManager()
    rest_handlers = [
        _make_rest_handler(tool) for tool in dispatcher.tools.values() if not tool.requires_session
    ]

    api_router = Router(
        path=f"/{SERVER_NAME}",
        route_handlers=rest_handlers,
        tags=[SERVER_NAME],
    )

    mcp_router = Router(
        path="/mcp",
        route_handlers=[_make_mcp_handler(dispatcher, sessions)],
        tags=["MCP"],
    )

    return Litestar(
        route_handlers=[api_router, mcp_router],
        openapi_config=OpenAPIConfig(
            title="TEESwap",
            version=SERVER_VERSION,
            description="Cross-chain swap aggregator in a verified TEE",
        ),
    )
