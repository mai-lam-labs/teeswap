"""HTTP transport — exposes a TeeSwap instance via Litestar.

Litestar only routes. Request bodies are parsed here and hydrated with
HasFromDict.from_dict, the same path MCP uses, never by Litestar's decoder.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from types import GenericAlias
from typing import TYPE_CHECKING, Any

from dacite import DaciteError
from litestar import Litestar, MediaType, Request, Response, Router, post
from litestar.exceptions import HTTPException
from litestar.openapi import OpenAPIConfig
from litestar.openapi.spec import OpenAPIMediaType, Operation, RequestBody
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_202_ACCEPTED,
    HTTP_400_BAD_REQUEST,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from .common import PKG_NAME, PKG_VERSION, TeeSwapError
from .dashboard import create_dashboard_router, create_invoice_router
from .execution.invoice import InvoiceNotFoundError
from .mcp import (
    AnyTool,
    JsonRpcNotification,
    PaidTool,
    ToolDefinition,
    ToolOutcome,
    handle_mcp_notification,
    handle_mcp_request,
    parse_message,
)
from .response import ToolResponse
from .schema import SCHEMA_PLUGINS, schema_object_for_type
from .wire import HasFromDict, WireError, decode_object, encode
from .x402 import (
    HTTP_PAYMENT_REQUIRED_HEADER,
    HTTP_PAYMENT_RESPONSE_HEADER,
    HTTP_PAYMENT_SIGNATURE_HEADER,
    PaymentPayload,
    ResourceInfo,
    X402PaymentResult,
    X402PaymentSpec,
)

if TYPE_CHECKING:
    from .instance import TeeSwap

logger = logging.getLogger(__name__)


class RequestError(TeeSwapError):
    pass


def _hydrate[T](parse: Callable[[dict[str, Any]], T], raw: bytes) -> T:
    try:
        return parse(decode_object(raw))
    except (WireError, DaciteError, ValueError, TypeError) as e:
        raise RequestError(str(e)) from e


# --- Error bodies: ours, not Litestar's ---


def _error_response(status_code: int, message: str) -> Response[bytes]:
    return Response(
        content=encode({"error": message}), status_code=status_code, media_type=MediaType.JSON
    )


def _on_error(_: Request[Any, Any, Any], exc: Exception) -> Response[bytes]:
    match exc:
        case RequestError():
            return _error_response(HTTP_400_BAD_REQUEST, str(exc))
        case InvoiceNotFoundError():
            return _error_response(HTTP_404_NOT_FOUND, str(exc))
        case TeeSwapError():
            return _error_response(HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
        case HTTPException():
            return _error_response(exc.status_code, exc.detail)
        case _:
            logger.error("unhandled error", exc_info=exc)
            return _error_response(HTTP_500_INTERNAL_SERVER_ERROR, "internal error")


def _operation_with_request_body(input_type: type[HasFromDict]) -> type[Operation]:
    """OpenAPI entry for a tool route, documenting the body we hydrate ourselves."""
    body = RequestBody(
        required=True,
        content={MediaType.JSON: OpenAPIMediaType(schema=schema_object_for_type(input_type))},
    )

    @dataclass
    class ToolOperation(Operation):
        def __post_init__(self) -> None:
            self.request_body = body

    return ToolOperation


# --- x402 HTTP transport (see docs/X402.md) ---


def _payment_from_headers(request: Request[Any, Any, Any]) -> PaymentPayload | None:
    header = request.headers.get(HTTP_PAYMENT_SIGNATURE_HEADER)
    if header is None:
        return None
    try:
        return PaymentPayload.from_dict(decode_object(base64.b64decode(header, validate=True)))
    except (WireError, DaciteError, ValueError, TypeError) as e:
        raise RequestError(f"invalid {HTTP_PAYMENT_SIGNATURE_HEADER} header: {e}") from e


def _payment_required_response(
    terms: X402PaymentSpec, request: Request[Any, Any, Any], defn: ToolDefinition
) -> Response[bytes]:
    required = terms.required(
        ResourceInfo(
            url=str(request.url), description=defn.description, mimeType="application/json"
        )
    )
    return Response(
        content=encode({"error": required.error}),
        status_code=HTTP_402_PAYMENT_REQUIRED,
        media_type=MediaType.JSON,
        headers={HTTP_PAYMENT_REQUIRED_HEADER: base64.b64encode(encode(required)).decode("ascii")},
    )


async def _execute(tool: AnyTool, args: Any, request: Request[Any, Any, Any]) -> ToolOutcome:
    if isinstance(tool, PaidTool):
        return await tool.execute(args, _payment_from_headers(request))
    # a payment sent to a free tool is ignored: never settled, never charged
    return await tool.execute(args)


def _make_rest_handler(tool: AnyTool) -> Any:
    defn = tool.definition

    async def handler(request: Request[Any, Any, Any]) -> Response[Any]:
        args = _hydrate(defn.input_type.from_dict, await request.body())
        outcome = await _execute(tool, args, request)
        match outcome:
            case X402PaymentSpec():
                return _payment_required_response(outcome, request, defn)
            case X402PaymentResult():
                body, media_type = outcome.response.to_rest()
                settlement = base64.b64encode(encode(outcome.settlement)).decode("ascii")
                return Response(
                    content=body,
                    status_code=HTTP_200_OK,
                    media_type=media_type,
                    headers={HTTP_PAYMENT_RESPONSE_HEADER: settlement},
                )
            case ToolResponse():
                body, media_type = outcome.to_rest()
                return Response(content=body, status_code=HTTP_200_OK, media_type=media_type)

    short_name = defn.name.removeprefix(f"{PKG_NAME}_")
    handler.__name__ = short_name
    handler.__qualname__ = short_name
    # built at runtime, so Litestar derives the OpenAPI response schema from the tool's output type
    handler.__annotations__["return"] = GenericAlias(Response, (defn.output_type,))

    return post(
        path=defn.rest_path,
        status_code=HTTP_200_OK,
        operation_class=_operation_with_request_body(defn.input_type),
        summary=short_name,
        description=defn.description,
    )(handler)


def _make_mcp_handler(instance: TeeSwap) -> Any:
    @post(
        path="/",
        summary="MCP JSON-RPC",
        description="MCP JSON-RPC endpoint (stateful + stateless). "
        "Supports initialize, server/discover, tools/list, tools/call, and verifiable-tools/call.",
    )
    async def mcp_handler(request: Request[Any, Any, Any]) -> Response[Any]:
        rpc = _hydrate(parse_message, await request.body())
        if isinstance(rpc, JsonRpcNotification):
            handle_mcp_notification(rpc)
            return Response(content=b"", status_code=HTTP_202_ACCEPTED, media_type=MediaType.TEXT)
        method = request.headers.get("Mcp-Method", rpc.method)
        session_id = request.headers.get("Mcp-Session-Id")

        mcp_result = await handle_mcp_request(
            instance.dispatcher, instance.sessions, method, rpc, session_id
        )

        resp = Response(
            content=encode(mcp_result.body),
            status_code=HTTP_200_OK,
            media_type=MediaType.JSON,
        )
        is_stateful = session_id is not None or method == "initialize"
        if is_stateful and mcp_result.session_id is not None:
            resp.headers["Mcp-Session-Id"] = mcp_result.session_id
        return resp

    return mcp_handler


def make_http_app(instance: TeeSwap) -> Litestar:
    rest_handlers = [
        _make_rest_handler(tool)
        for tool in instance.dispatcher.tools.values()
        if not tool.requires_session
    ]

    api_router = Router(
        path=f"/{PKG_NAME}",
        route_handlers=rest_handlers,
        tags=[PKG_NAME],
    )

    mcp_router = Router(
        path="/mcp",
        route_handlers=[_make_mcp_handler(instance)],
        tags=["MCP"],
    )

    routers = [
        api_router,
        mcp_router,
        create_invoice_router(instance.invoice_registry),
        create_dashboard_router(
            instance.config.operator_password,
            instance.facilitator_monitor,
            instance.rpc_monitor,
            instance.invoice_registry,
        ),
    ]

    async def on_startup() -> None:
        instance.start_background_tasks()

    async def on_shutdown() -> None:
        instance.stop_background_tasks()

    return Litestar(
        route_handlers=routers,
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        plugins=SCHEMA_PLUGINS,
        exception_handlers={Exception: _on_error},
        openapi_config=OpenAPIConfig(
            title=PKG_NAME,
            version=PKG_VERSION,
            description="Cross-chain swap aggregator in a verified TEE",
        ),
    )
