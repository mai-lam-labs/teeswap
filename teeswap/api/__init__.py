"""TeeSwap's tools as typed Python calls, over any of its interfaces.

Api is the interface: one method per tool, taking and returning the tools' own
types, raising the same TeeSwapError subclasses whichever way it's reached.

- LocalApi: in process, calling the tools directly (TeeSwap as a library).
- RestApi: a client for the REST interface (x402 in HTTP headers).
- McpApi: a client for the MCP interface (x402 in _meta).

Paying for an x402 tool is a Payer's business, not the Api's: accept_x402 says what
to pay and carries a payment; accept_paid does the whole exchange with a Payer.
"""

from .base import Api, PaidAccept
from .local import LocalApi
from .mcp import McpApi
from .rest import RestApi

__all__ = ["Api", "LocalApi", "McpApi", "PaidAccept", "RestApi"]
