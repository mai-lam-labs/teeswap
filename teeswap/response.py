import abc
import base64
from typing import Any, override

from litestar import MediaType

from .wire import WireStruct, encode


class ToolResponse(abc.ABC):
    @abc.abstractmethod
    def to_mcp_content(self) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def to_rest(self) -> tuple[Any, str]: ...


class JsonResponse(ToolResponse):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    @override
    def to_mcp_content(self) -> list[dict[str, Any]]:
        return [{"type": "text", "text": encode(self.data).decode()}]

    @override
    def to_rest(self) -> tuple[bytes, str]:
        return encode(self.data), MediaType.JSON


class TextResponse(ToolResponse):
    def __init__(self, text: str) -> None:
        self.text = text

    @override
    def to_mcp_content(self) -> list[dict[str, Any]]:
        return [{"type": "text", "text": self.text}]

    @override
    def to_rest(self) -> tuple[str, str]:
        return self.text, "text/plain"


class FileResponse(ToolResponse):
    def __init__(self, data: bytes, filename: str, media_type: str) -> None:
        self.data = data
        self.filename = filename
        self.media_type = media_type

    @override
    def to_mcp_content(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "resource",
                "resource": {
                    "uri": f"file:///{self.filename}",
                    "mimeType": self.media_type,
                    "blob": base64.b64encode(self.data).decode(),
                },
            }
        ]

    @override
    def to_rest(self) -> tuple[bytes, str]:
        return self.data, self.media_type


class CompositeResponse(ToolResponse):
    def __init__(self, parts: list[ToolResponse]) -> None:
        self.parts = parts

    @override
    def to_mcp_content(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for part in self.parts:
            blocks.extend(part.to_mcp_content())
        return blocks

    @override
    def to_rest(self) -> tuple[Any, str]:
        for part in self.parts:
            body, mt = part.to_rest()
            if mt == "application/json":
                return body, mt
        return self.parts[0].to_rest()


class DataclassResponse(WireStruct, ToolResponse):
    """A dataclass tool result, encoded once with wire.encode.

    MCP text and REST body are the same bytes, so the output commitment
    covers exactly what a REST client receives.
    """

    @override
    def to_mcp_content(self) -> list[dict[str, Any]]:
        return [{"type": "text", "text": encode(self).decode()}]

    @override
    def to_rest(self) -> tuple[bytes, str]:
        return encode(self), MediaType.JSON
