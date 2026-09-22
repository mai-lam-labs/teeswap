from typing import override

from teeswap.mcp import Dispatcher, Tool, ToolDefinition
from teeswap.response import JsonResponse
from teeswap.schema import schema_for_type
from teeswap.types import AcceptRequest, QuoteRequest, StatusRequest


class DummyQuoteTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_quote",
            description="Get a quote.",
            input_type=QuoteRequest,
            annotations={"readOnly": True, "openWorld": True},
        )

    @override
    async def execute(self, args: QuoteRequest) -> JsonResponse:
        return JsonResponse({"status": "ok"})


def test_quote_request_schema_has_properties() -> None:
    schema = schema_for_type(QuoteRequest)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "input" in props
    assert "outputs" in props


def test_accept_request_schema() -> None:
    schema = schema_for_type(AcceptRequest)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "quote_id" in props


def test_status_request_schema() -> None:
    schema = schema_for_type(StatusRequest)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "quote_id" in props


def test_tool_definition_derives_schema_from_type() -> None:
    tool = DummyQuoteTool()
    schema = tool.definition.input_schema
    assert schema["type"] == "object"
    assert "input" in schema["properties"]


def test_dispatcher_tools_list() -> None:
    dispatcher = Dispatcher()
    dispatcher.register(DummyQuoteTool())
    listing = dispatcher.tools_list()
    assert len(listing) == 1
    assert listing[0]["name"] == "teeswap_quote"
    assert "properties" in listing[0]["inputSchema"]
