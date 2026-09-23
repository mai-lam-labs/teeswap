from dataclasses import dataclass

from .blockchain.chains import Chain, ChainFamily
from .blockchain.rpc import RpcConfig
from .types import HasFromDict


@dataclass(frozen=True, slots=True)
class FeeConfig:
    quote_fee_usd: str = "0.005"
    swap_fee_bps: int = 10
    max_slippage_bps: int = 500


@dataclass(frozen=True, slots=True)
class Operator:
    """The invoice issuer. Every field is optional; the invoice shows whatever is set."""

    legal_name: str | None = None
    registration_number: str | None = None
    postal_address: str | None = None
    extra: str | None = None


@dataclass(frozen=True, slots=True)
class FacilitatorConfig:
    url: str
    poll_interval: float = 300.0


_DEFAULT_FACILITATORS: tuple[FacilitatorConfig, ...] = (
    FacilitatorConfig(url="https://x402.org/facilitator"),
    FacilitatorConfig(url="https://facilitator.payai.network"),
    FacilitatorConfig(url="https://x402.polygon.technology"),
    FacilitatorConfig(url="https://facilitator.x402.rs"),
    FacilitatorConfig(url="https://facilitator.heurist.xyz"),
    FacilitatorConfig(url="https://facilitator.ultravioletadao.xyz"),
    FacilitatorConfig(url="https://x402.dexter.cash"),
)


_E, _S = ChainFamily.EVM, ChainFamily.SVM
_ST, _AL = ChainFamily.STELLAR, ChainFamily.ALGORAND
_NE, _SU = ChainFamily.NEAR, ChainFamily.SUI
_XR, _TR = ChainFamily.XRPL, ChainFamily.TRON
_AP = ChainFamily.APTOS
_T = True
# fmt: off
_DEFAULT_CHAINS: tuple[Chain, ...] = (
    # EVM mainnets
    Chain("eip155:1", _E, "Ethereum", "eth", "ETH", 18),
    Chain("eip155:10", _E, "Optimism", "optimism", "ETH", 18),
    Chain("eip155:56", _E, "BNB Chain", "bsc", "BNB", 18),
    Chain("eip155:100", _E, "Gnosis", "gnosis", "xDAI", 18),
    Chain("eip155:130", _E, "Unichain", "unichain", "ETH", 18),
    Chain("eip155:137", _E, "Polygon", "polygon", "POL", 18),
    Chain("eip155:143", _E, "Monad", "monad", "MON", 18),
    Chain("eip155:196", _E, "X Layer", "xlayer", "OKB", 18),
    Chain("eip155:324", _E, "zkSync Era", "zksync", "ETH", 18),
    Chain("eip155:480", _E, "World Chain", "world", "ETH", 18),
    Chain("eip155:988", _E, "Stable", "stable", "USDT0", 18),
    Chain("eip155:999", _E, "Hyperliquid", "hyperevm", "HYPE", 18),
    Chain("eip155:1329", _E, "Sei", "sei", "SEI", 18),
    Chain("eip155:5042", _E, "Arc", "arc", "ARC", 18),
    Chain("eip155:8453", _E, "Base", "base", "ETH", 18),
    Chain("eip155:42161", _E, "Arbitrum One", "arbitrum", "ETH", 18),
    Chain("eip155:42220", _E, "Celo", "celo", "CELO", 18),
    Chain("eip155:43114", _E, "Avalanche C", "avalanche", "AVAX", 18),
    Chain("eip155:59144", _E, "Linea", "linea", "ETH", 18),
    Chain("eip155:534352", _E, "Scroll", "scroll", "ETH", 18),
    Chain("eip155:1187947933", _E, "SKALE Base", "skale-base", "CREDIT", 18),
    # EVM testnets
    Chain("eip155:97", _E, "BNB Testnet", "bsc-testnet", "tBNB", 18, _T),
    Chain("eip155:1301", _E, "Unichain Sepolia", "unichain-sepolia", "ETH", 18, _T),
    Chain("eip155:1328", _E, "Sei Testnet", "sei-testnet", "SEI", 18, _T),
    Chain("eip155:1952", _E, "X Layer Testnet", "xlayer-testnet", "OKB", 18, _T),
    Chain("eip155:5042002", _E, "Arc Testnet", "arc-testnet", "ARC", 18, _T),
    Chain("eip155:6343", _E, "ARC Testnet 2", "arc-testnet-2", "ARC", 18, _T),
    Chain("eip155:10143", _E, "Monad Testnet", "monad-testnet", "MON", 18, _T),
    Chain("eip155:11155111", _E, "Ethereum Sepolia", "eth-sepolia", "ETH", 18, _T),
    Chain("eip155:11155420", _E, "OP Sepolia", "op-sepolia", "ETH", 18, _T),
    Chain("eip155:16602", _E, "0G Galileo", "0g-testnet", "A0GI", 18, _T),
    Chain("eip155:43113", _E, "Avalanche Fuji", "avalanche-fuji", "AVAX", 18, _T),
    Chain("eip155:44787", _E, "Celo Alfajores", "celo-testnet", "CELO", 18, _T),
    Chain("eip155:46630", _E, "Robinhood Testnet", "robinhood-testnet", "ETH", 18, _T),
    Chain("eip155:72344", _E, "Radius Testnet", "radius-testnet", "RUSD", 18, _T),
    Chain("eip155:80002", _E, "Polygon Amoy", "polygon-amoy", "POL", 18, _T),
    Chain("eip155:84532", _E, "Base Sepolia", "base-sepolia", "ETH", 18, _T),
    Chain("eip155:324705682", _E, "SKALE Base Sepolia", "skale-base-sepolia", "CREDIT", 18, _T),
    Chain("eip155:421614", _E, "Arbitrum Sepolia", "arbitrum-sepolia", "ETH", 18, _T),
    Chain("eip155:713715", _E, "Sei Devnet", "sei-devnet", "SEI", 18, _T),
    # Solana
    Chain("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", _S, "Solana", "solana", "SOL", 9),
    Chain("solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", _S, "Solana Devnet", "solana-devnet", "SOL", 9, _T),
    # Stellar
    Chain("stellar:pubnet", _ST, "Stellar", "stellar", "XLM", 7),
    Chain("stellar:testnet", _ST, "Stellar Testnet", "stellar-testnet", "XLM", 7, _T),
    # Algorand
    Chain("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73k", _AL, "Algorand", "algorand", "ALGO", 6),
    Chain("algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe", _AL, "Algorand Testnet", "algorand-testnet", "ALGO", 6, _T),
    # NEAR
    Chain("near:mainnet", _NE, "NEAR", "near", "NEAR", 24),
    Chain("near:testnet", _NE, "NEAR Testnet", "near-testnet", "NEAR", 24, _T),
    # Sui
    Chain("sui:mainnet", _SU, "Sui", "sui", "SUI", 9),
    Chain("sui:testnet", _SU, "Sui Testnet", "sui-testnet", "SUI", 9, _T),
    # XRPL
    Chain("xrpl:0", _XR, "XRPL", "xrpl", "XRP", 6),
    Chain("xrpl:1", _XR, "XRPL Testnet", "xrpl-testnet", "XRP", 6, _T),
    # Tron
    Chain("tron:0x2b6653dc", _TR, "Tron", "tron", "TRX", 6),
    # Aptos
    Chain("aptos:2", _AP, "Aptos Testnet", "aptos-testnet", "APT", 8, _T),
)
# fmt: on


# fmt: off
_DEFAULT_RPCS: tuple[RpcConfig, ...] = (
    # EVM mainnets
    RpcConfig("eip155:1", ("https://ethereum-rpc.publicnode.com", "https://cloudflare-eth.com")),
    RpcConfig("eip155:10", ("https://mainnet.optimism.io", "https://optimism-rpc.publicnode.com")),
    RpcConfig("eip155:56", ("https://bsc-dataseed1.bnbchain.org", "https://bsc-rpc.publicnode.com")),
    RpcConfig("eip155:100", ("https://rpc.gnosischain.com", "https://gnosis-rpc.publicnode.com")),
    RpcConfig("eip155:130", ("https://mainnet.unichain.org", "https://unichain-rpc.publicnode.com")),
    RpcConfig("eip155:137", ("https://polygon-bor-rpc.publicnode.com", "https://1rpc.io/matic")),
    RpcConfig("eip155:143", ("https://rpc.monad.xyz",)),
    RpcConfig("eip155:196", ("https://rpc.xlayer.tech", "https://xlayerrpc.okx.com")),
    RpcConfig("eip155:324", ("https://mainnet.era.zksync.io",)),
    RpcConfig("eip155:480", ("https://worldchain-mainnet.g.alchemy.com/public",)),
    RpcConfig("eip155:988", ("https://rpc.stable.xyz",)),
    RpcConfig("eip155:999", ("https://rpc.hyperliquid.xyz/evm",)),
    RpcConfig("eip155:1329", ("https://evm-rpc.sei-apis.com", "https://sei-evm-rpc.publicnode.com")),
    RpcConfig("eip155:5042", ("https://rpc.mainnet.arc.io",)),
    RpcConfig("eip155:8453", ("https://mainnet.base.org", "https://base-rpc.publicnode.com")),
    RpcConfig("eip155:42161", ("https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com")),
    RpcConfig("eip155:42220", ("https://forno.celo.org", "https://celo-rpc.publicnode.com")),
    RpcConfig("eip155:43114", ("https://api.avax.network/ext/bc/C/rpc", "https://avalanche-c-chain-rpc.publicnode.com")),
    RpcConfig("eip155:59144", ("https://rpc.linea.build", "https://linea-rpc.publicnode.com")),
    RpcConfig("eip155:534352", ("https://rpc.scroll.io", "https://scroll-rpc.publicnode.com")),
    RpcConfig("eip155:1187947933", ("https://skale-base.skalenodes.com/v1/base",)),
    # Solana
    RpcConfig("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com")),
    # Stellar
    RpcConfig("stellar:pubnet", ("https://horizon.stellar.org",)),
    # Algorand
    RpcConfig("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73k", ("https://mainnet-api.4160.nodely.dev", "https://mainnet-api.algonode.cloud")),
    # Hedera
    # NEAR
    RpcConfig("near:mainnet", ("https://rpc.mainnet.near.org", "https://free.rpc.fastnear.com")),
    # Sui
    RpcConfig("sui:mainnet", ("https://sui-rpc.publicnode.com",)),
    # XRPL
    RpcConfig("xrpl:0", ("https://s1.ripple.com:51234", "https://s2.ripple.com:51234")),
    # Tron
    RpcConfig("tron:0x2b6653dc", ("https://api.trongrid.io/jsonrpc", "https://tron-rpc.publicnode.com/jsonrpc")),
)
# fmt: on


@dataclass(frozen=True, slots=True)
class TeeSwapConfig(HasFromDict):
    facilitators: tuple[FacilitatorConfig, ...] = _DEFAULT_FACILITATORS
    chains: tuple[Chain, ...] = _DEFAULT_CHAINS
    rpcs: tuple[RpcConfig, ...] = _DEFAULT_RPCS
    operator_password: str = "password"
    operator: Operator = Operator()
