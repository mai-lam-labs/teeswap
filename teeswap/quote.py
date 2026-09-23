"""Quote computation — estimates gas, fees, and output amounts.

The quote is pro-rata: it shows what the user can expect, not a locked price.
Actual amounts depend on execution-time gas prices.

Inputs are grouped by token: 0.5 ETH + 1.0 ETH is the same request as 1.5 ETH.
The quote carries the grouped form, so each invoice input is a distinct token.
"""

from collections.abc import Callable

from .blockchain.chains import Chain, ChainFamily
from .blockchain.rpc import jsonrpc
from .config import FeeConfig
from .http import BaseHttpClient
from .invoice import QUOTE_TTL, InvoiceId
from .protocol import NoRouteError
from .types import (
    Amount,
    Balance,
    QuoteRequest,
    QuoteResponse,
    Timestamp,
    Token,
    TokenAmount,
    Url,
)

ETH_TRANSFER_GAS = 21_000


async def compute_quote(
    client: BaseHttpClient,
    rpc_url_for: Callable[[Chain], Url],
    request: QuoteRequest,
    fee_config: FeeConfig,
) -> QuoteResponse:
    inputs = _group_by_token(request.inputs)
    token = _native_transfer_token(inputs, request.outputs)

    gas_price = await _get_gas_price(client, rpc_url_for(token.chain))
    n_transfers = len(request.outputs)
    total_gas_wei = gas_price * ETH_TRANSFER_GAS * n_transfers

    input_amount = inputs[0].amount
    fee_amount = input_amount * fee_config.swap_fee_bps // 10_000

    available = input_amount - total_gas_wei - fee_amount
    if available <= 0:
        raise ValueError(
            f"input {input_amount} insufficient for gas ({total_gas_wei}) + fee ({fee_amount})"
        )

    total_requested = sum(o.amount.amount for o in request.outputs)
    output_estimates = tuple(
        Balance(
            amount=TokenAmount(
                token=token, amount=Amount(available * o.amount.amount // total_requested)
            ),
            address=o.address,
        )
        for o in request.outputs
    )

    quote_id = InvoiceId.generate()

    return QuoteResponse(
        quote_id=quote_id,
        inputs=inputs,
        outputs=output_estimates,
        gas=TokenAmount(token=token, amount=Amount(total_gas_wei)),
        fee=TokenAmount(token=token, amount=Amount(fee_amount)),
        expires_at=Timestamp.now() + QUOTE_TTL,
    )


def _group_by_token(amounts: tuple[TokenAmount, ...]) -> tuple[TokenAmount, ...]:
    totals: dict[Token, int] = {}
    for a in amounts:
        totals[a.token] = totals.get(a.token, 0) + a.amount
    return tuple(TokenAmount(token=t, amount=Amount(n)) for t, n in totals.items())


def _native_transfer_token(inputs: tuple[TokenAmount, ...], outputs: tuple[Balance, ...]) -> Token:
    """The single token moved by a same-chain native transfer — the only route executed today."""
    if len(inputs) != 1:
        raise NoRouteError(f"expected exactly one input token, got {len(inputs)}")
    token = inputs[0].token
    if token.chain.family != ChainFamily.EVM or token.contract is not None:
        raise NoRouteError(f"only native EVM transfers are supported, not {token.symbol}")
    if not outputs:
        raise NoRouteError("no outputs")
    for out in outputs:
        if out.amount.token != token:
            raise NoRouteError(
                f"output {out.amount.token.symbol} differs from input {token.symbol}"
            )
    return token


async def _get_gas_price(client: BaseHttpClient, rpc_url: Url) -> int:
    result = await jsonrpc(client, rpc_url, "eth_gasPrice")
    return int(result, 16)
