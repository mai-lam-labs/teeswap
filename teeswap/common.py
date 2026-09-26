from importlib.metadata import metadata
from typing import Any, ClassVar

_PKG = metadata(__package__ or __name__)
PKG_NAME: str = _PKG["Name"]
PKG_VERSION: str = _PKG["Version"]


DEFAULT_HOST = "0.0.0.0"  # noqa: S104
DEFAULT_PORT = 8402


class TeeSwapError(Exception):
    """A domain failure. Its class name is its code on the wire, so a client on the
    other side of REST or MCP raises the same class (see response.ErrorResponse)."""

    _by_code: ClassVar[dict[str, type[TeeSwapError]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        TeeSwapError._by_code[cls.__name__] = cls

    @property
    def code(self) -> str:
        return type(self).__name__

    @classmethod
    def from_code(cls, code: str, message: str) -> TeeSwapError:
        """The error a peer reported: its own class if this side knows it."""
        return cls._by_code.get(code, TeeSwapError)(message)
