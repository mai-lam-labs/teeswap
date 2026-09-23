"""Quote: Mai's estimate, from a dry run of her provisional plan.

The planner decides the operations; each operation estimates its own costs; the
quote allocates what's left to the outputs pro rata. It shows what the user can
expect and the plan Mai expects to follow, not a locked price or a fixed script.

Inputs are grouped by token: 0.5 ETH + 1.0 ETH is the same request as 1.5 ETH.
The quote carries the grouped form, so each invoice input is a distinct token.
"""

from collections.abc import Callable

from eth_typing import ChecksumAddress

from ..blockchain.chains import Chain
from ..blockchain.evm import EvmChain
from ..blockchain.rpc import JsonRpcError
from ..common import TeeSwapError
from ..http import BaseHttpClient
from ..types import Amount, Balance, QuoteRequest, QuoteResponse, Timestamp, Token, TokenAmount, Url
from .invoice import QUOTE_TTL, Funding, InvoiceId
from .planner import FacilitatorsFor, provisional_plan


class QuoteError(TeeSwapError):
    pass


async def compute_quote(
    client: BaseHttpClient,
    rpc_url_for: Callable[[Chain], Url],
    request: QuoteRequest,
    quote_id: InvoiceId,
    job_address: ChecksumAddress,
    funding: Funding,
    facilitators_for: FacilitatorsFor,
) -> QuoteResponse:
    """Quote a job whose funds will be held at `job_address` (not yet funded)."""
    inputs = _group_by_token(request.inputs)

    def evm_for(chain: Chain) -> EvmChain:
        return EvmChain(chain, client, rpc_url_for(chain))

    costs: list[TokenAmount] = []
    for operation in provisional_plan(
        inputs, request.outputs, job_address, funding, facilitators_for
    ):
        try:
            costs += await operation.estimate_costs(evm_for, job_address)
        except JsonRpcError as e:
            raise QuoteError(f"cannot {operation.description}: {e.rpc_message}") from e

    (supplied,) = inputs  # the planner routes exactly one input token
    spent = sum(c.amount for c in costs if c.token == supplied.token)
    available = supplied.amount - spent
    if available <= 0:
        raise QuoteError(f"input {supplied.amount} insufficient for costs ({spent})")

    total_requested = sum(o.amount.amount for o in request.outputs)
    if total_requested == 0:
        raise QuoteError("outputs must request a non-zero total amount")
    outputs = tuple(
        Balance(
            amount=TokenAmount(
                token=o.amount.token,
                amount=Amount(available * o.amount.amount // total_requested),
            ),
            address=o.address,
        )
        for o in request.outputs
    )

    return QuoteResponse(
        quote_id=quote_id,
        inputs=inputs,
        outputs=outputs,
        gas=TokenAmount(token=supplied.token, amount=Amount(spent)),
        plan=tuple(
            op.description
            for op in provisional_plan(inputs, outputs, job_address, funding, facilitators_for)
        ),
        expires_at=Timestamp.now() + QUOTE_TTL,
    )


def _group_by_token(amounts: tuple[TokenAmount, ...]) -> tuple[TokenAmount, ...]:
    totals: dict[Token, int] = {}
    for a in amounts:
        totals[a.token] = totals.get(a.token, 0) + a.amount
    return tuple(TokenAmount(token=t, amount=Amount(n)) for t, n in totals.items())
