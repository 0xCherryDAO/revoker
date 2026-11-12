from dataclasses import dataclass
from os import environ


PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"


@dataclass(frozen=True)
class Network:
    chain_id: int
    rpc: str
    start_block: int = 0
    block_span: int = 50_000
    permit2: str | None = PERMIT2_ADDRESS
    index_first: bool = False


# Override the public endpoints here or via REVOKER_RPC_<NETWORK>.
NETWORKS = {
    "ethereum": Network(1, "https://mainnet.gateway.tenderly.co", block_span=1_000_000),
    "base": Network(8453, "https://base.api.pocket.network", block_span=100_000),
    "optimism": Network(10, "https://optimism.gateway.tenderly.co", block_span=1_000_000),
    "arbitrum": Network(42161, "https://arbitrum.gateway.tenderly.co", block_span=1_000_000),
    "polygon": Network(137, "https://polygon.gateway.tenderly.co", block_span=1_000_000),
    # Full history at 5,000 blocks per call would take tens of thousands of RPC requests.
    "bsc": Network(56, "https://bsc.api.pocket.network", block_span=5_000, index_first=True),
    "avalanche": Network(43114, "https://avalanche.gateway.tenderly.co", block_span=1_000_000),
}

RPC_TIMEOUT = 30
RECEIPT_TIMEOUT = 180


def rpc_url(name: str, network: Network) -> str:
    return environ.get(f"REVOKER_RPC_{name.upper()}", network.rpc)
