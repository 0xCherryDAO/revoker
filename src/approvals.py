from dataclasses import dataclass

from web3 import Web3
from web3.exceptions import ContractLogicError, OffchainLookup

from src.discovery import ApprovalCandidate


ZERO_ADDRESS = "0x" + "00" * 20
MAX_PERMIT2_NONCE = (1 << 48) - 1


@dataclass(frozen=True)
class ApprovalState:
    spender: str
    amount: int
    nonce: int | None = None


def _calldata(w3: Web3, name: str, types: tuple, args: tuple) -> bytes:
    signature = f"{name}({','.join(types)})"
    return w3.keccak(text=signature)[:4] + w3.codec.encode(types, args)


def _read(w3: Web3, contract: str, name: str, types: tuple, args: tuple, outputs: tuple) -> tuple:
    result = w3.eth.call(
        {"to": contract, "data": _calldata(w3, name, types, args)},
        ccip_read_enabled=False,
    )
    if len(result) != 32 * len(outputs):
        raise ValueError(f"Invalid ABI response for {name}")
    return w3.codec.decode(outputs, result)


def read_approval(w3: Web3, owner: str, candidate: ApprovalCandidate) -> ApprovalState:
    kind, contract, spender = candidate.kind, candidate.contract, candidate.spender
    address_pair = ("address", "address")
    if kind == "erc20":
        amount, = _read(w3, contract, "allowance", address_pair, (owner, spender), ("uint256",))
        return ApprovalState(spender, amount)
    if kind == "operator":
        enabled, = _read(w3, contract, "isApprovedForAll", address_pair, (owner, spender), ("bool",))
        return ApprovalState(spender, int(enabled))
    if kind == "erc721":
        try:
            holder, = _read(w3, contract, "ownerOf", ("uint256",), (candidate.token_id,), ("address",))
        except OffchainLookup:
            raise
        except ContractLogicError:
            # Standard ERC-721 ownerOf reverts for an invalid or burned token ID.
            return ApprovalState(ZERO_ADDRESS, 0)
        if holder.lower() != owner.lower():
            return ApprovalState(ZERO_ADDRESS, 0)
        approved, = _read(w3, contract, "getApproved", ("uint256",), (candidate.token_id,), ("address",))
        return ApprovalState(Web3.to_checksum_address(approved), int(approved != ZERO_ADDRESS))
    if kind == "permit2":
        amount, _, nonce = _read(
            w3, contract, "allowance", ("address", "address", "address"),
            (owner, candidate.token, spender), ("uint160", "uint48", "uint48"),
        )
        return ApprovalState(spender, amount, nonce)
    raise ValueError(f"Unknown approval kind: {kind}")


def revocation_data(w3: Web3, candidate: ApprovalCandidate) -> bytes:
    kind, spender = candidate.kind, candidate.spender
    if kind == "erc20":
        return _calldata(w3, "approve", ("address", "uint256"), (spender, 0))
    if kind == "erc721":
        return _calldata(w3, "approve", ("address", "uint256"), (ZERO_ADDRESS, candidate.token_id))
    if kind == "operator":
        return _calldata(w3, "setApprovalForAll", ("address", "bool"), (spender, False))
    if kind == "permit2":
        return _calldata(w3, "lockdown", ("(address,address)[]",), ([(candidate.token, spender)],))
    raise ValueError(f"Unknown approval kind: {kind}")


def permit2_invalidation_data(w3: Web3, candidate: ApprovalCandidate, nonce: int) -> bytes:
    return _calldata(
        w3, "invalidateNonces", ("address", "address", "uint48"),
        (candidate.token, candidate.spender, nonce + 1),
    )
