"""Quote computation — estimates gas, fees, and output amounts.

The quote is pro-rata: it shows what the user can expect, not a locked price.
Actual amounts depend on execution-time gas prices.
"""

from datetime import UTC, datetime, timedelta

from .blockchain.rpc import jsonrpc
from .config import FeeConfig
from .http import BaseHttpClient
from .invoice import InvoiceId
from .types import GasEstimate, OutputEstimate, QuoteRequest, QuoteResponse, SecureUrl, TokenAmount

QUOTE_TTL = timedelta(minutes=5)
ETH_TRANSFER_GAS = 21_000


async def compute_quote(
    client: BaseHttpClient,
    rpc_url: SecureUrl | str,
    request: QuoteRequest,
    fee_config: FeeConfig,
) -> QuoteResponse:
    gas_price = await _get_gas_price(client, rpc_url)
    n_transfers = len(request.outputs)
    total_gas_wei = gas_price * ETH_TRANSFER_GAS * n_transfers

    input_amount = request.input.amount
    fee_amount = input_amount * fee_config.swap_fee_bps // 10_000

    available = input_amount - total_gas_wei - fee_amount
    if available <= 0:
        raise ValueError(
            f"input {input_amount} insufficient for gas ({total_gas_wei}) + fee ({fee_amount})"
        )

    total_requested = sum(o.amount for o in request.outputs)
    output_estimates = tuple(
        OutputEstimate(
            recipient=o.recipient,
            token=o.token,
            amount=available * o.amount // total_requested,
        )
        for o in request.outputs
    )

    quote_id = InvoiceId.generate()

    return QuoteResponse(
        quote_id=quote_id,
        input=request.input,
        outputs=output_estimates,
        gas=GasEstimate(token=request.input.token, amount=total_gas_wei),
        fee=TokenAmount(token=request.input.token, amount=fee_amount),
        expires_at=datetime.now(UTC) + QUOTE_TTL,
    )


async def _get_gas_price(client: BaseHttpClient, rpc_url: SecureUrl | str) -> int:
    result = await jsonrpc(client, rpc_url, "eth_gasPrice")
    return int(result, 16)
