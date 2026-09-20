from dataclasses import dataclass, field

from .types import ChainId


@dataclass(frozen=True, slots=True)
class FeeConfig:
    quote_fee_usd: str = "0.005"
    swap_fee_bps: int = 10
    max_slippage_bps: int = 500


@dataclass(frozen=True, slots=True)
class AttestationConfig:
    vaportpm_attest_bin: str = "vaportpm-attest"
    vaportpm_pcr_extend_bin: str = "vaportpm"
    pcr_code_index: int = 14
    pcr_config_index: int = 15
    pcr_key_index: int = 16


@dataclass(frozen=True, slots=True)
class Config:
    chains: dict[str, ChainId] = field(default_factory=dict)
    cast_bin: str = "cast"
    solana_tools_bin: str = "solana-tools-lite"
    fees: FeeConfig = field(default_factory=FeeConfig)
    attestation: AttestationConfig = field(default_factory=AttestationConfig)
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8402
