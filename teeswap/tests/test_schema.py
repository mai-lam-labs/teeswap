from typing import Any, override

from teeswap.mcp import Dispatcher, Tool, ToolDefinition
from teeswap.response import JsonResponse, ToolResponse
from teeswap.schema import schema_for_type
from teeswap.types import QuoteRequest, RoutesFilter


class DummyQuoteTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_quote",
            description="Get a swap quote.",
            input_type=QuoteRequest,
            annotations={"readOnly": True, "openWorld": True},
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        return JsonResponse({"status": "ok"})


def test_quote_request_schema_has_properties() -> None:
    schema = schema_for_type(QuoteRequest)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "input_token" in props
    assert "input_chain" in props
    assert "input_amount" in props
    assert "recipient" in props


def test_quote_request_descriptions_propagate() -> None:
    schema = schema_for_type(QuoteRequest)
    props = schema["properties"]
    assert "description" in props["input_token"]
    assert "USDC" in props["input_token"]["description"]


def test_enum_field_produces_enum_values() -> None:
    schema = schema_for_type(QuoteRequest)
    props = schema["properties"]
    risk = props["risk_preference"]
    assert "enum" in risk
    assert "low" in risk["enum"]
    assert "high" in risk["enum"]


def test_tool_definition_derives_schema_from_type() -> None:
    tool = DummyQuoteTool()
    schema = tool.definition.input_schema
    assert schema["type"] == "object"
    assert "input_token" in schema["properties"]


def test_dispatcher_tools_list() -> None:
    dispatcher = Dispatcher()
    dispatcher.register(DummyQuoteTool())
    listing = dispatcher.tools_list()
    assert len(listing) == 1
    assert listing[0]["name"] == "teeswap_quote"
    assert "properties" in listing[0]["inputSchema"]


def test_routes_filter_all_optional() -> None:
    schema = schema_for_type(RoutesFilter)
    required = schema.get("required", [])
    assert len(required) == 0
