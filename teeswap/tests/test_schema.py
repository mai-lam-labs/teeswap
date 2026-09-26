import re
from dataclasses import dataclass
from typing import override

import pytest

from teeswap.mcp import Dispatcher, Tool, ToolDefinition
from teeswap.response import JsonResponse
from teeswap.schema import schema_for_type
from teeswap.types import HexStr, InvoiceRequest, QuoteRequest, TxHash
from teeswap.wire import HasFromDict
from teeswap.x402 import PaymentPayload


class DummyQuoteTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_quote",
            description="Get a quote.",
            input_type=QuoteRequest,
            output_type=JsonResponse,
            annotations={"readOnly": True, "openWorld": True},
        )

    @override
    async def execute(
        self, args: QuoteRequest, payment: PaymentPayload | None = None
    ) -> JsonResponse:
        return JsonResponse({"status": "ok"})


def test_quote_request_schema_has_properties() -> None:
    schema = schema_for_type(QuoteRequest)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "inputs" in props
    assert "outputs" in props


def test_invoice_request_schema() -> None:
    schema = schema_for_type(InvoiceRequest)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "quote_id" in props


def test_tool_definition_derives_schema_from_type() -> None:
    tool = DummyQuoteTool()
    schema = tool.definition.input_schema
    assert schema["type"] == "object"
    assert "inputs" in schema["properties"]


def test_dispatcher_tools_list() -> None:
    dispatcher = Dispatcher()
    dispatcher.register(DummyQuoteTool())
    listing = dispatcher.tools_list()
    assert len(listing) == 1
    assert listing[0]["name"] == "teeswap_quote"
    assert "properties" in listing[0]["inputSchema"]


@dataclass(frozen=True, slots=True)
class HexHolder(HasFromDict):
    key: TxHash
    blob: HexStr


def test_hexstr_schema_is_string_with_pattern() -> None:
    props = schema_for_type(HexHolder)["properties"]
    assert props["key"]["type"] == "string"
    assert props["key"]["pattern"] == TxHash.pattern()
    assert "32-byte" in props["key"]["description"]
    assert props["blob"]["type"] == "string"
    assert props["blob"]["pattern"] == HexStr.pattern()


@pytest.mark.parametrize(
    "value",
    [
        "0x" + "ab" * 32,
        "AB" * 32,
        "0x" + "ab" * 31,
        "0x" + "ab" * 33,
        "0x" + "ab" * 31 + "a",
        "0x" + "zz" * 32,
        "ab " * 32,
        "0x" + "ab" * 32 + "\n",
        "0X" + "ab" * 32,
        "",
    ],
)
def test_hexstr_pattern_matches_constructor(value: str) -> None:
    accepted = re.fullmatch(TxHash.pattern(), value) is not None
    try:
        TxHash(value)
        constructed = True
    except ValueError:
        constructed = False
    assert accepted == constructed


def test_hexstr_is_canonical() -> None:
    assert HexStr("ABcd") == "0xabcd"
    assert HexStr("0xABcd") == "0xabcd"
    assert HexStr("") == "0x"


def test_hexstr_rejects_non_str_json() -> None:
    with pytest.raises(TypeError, match="HexStr: expected str, got int"):
        HexHolder.from_dict({"key": "ab" * 32, "blob": 1234})
