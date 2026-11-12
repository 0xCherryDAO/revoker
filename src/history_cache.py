import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from hashlib import sha256
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile

from web3 import Web3

from src.discovery import ApprovalCandidate, ZERO_ADDRESS


_VERSION = 1
_BLOCK_HASH = re.compile(r"0x[0-9a-f]{64}")


@dataclass(frozen=True)
class HistoryCheckpoint:
    block: int
    block_hash: str
    candidates: dict[str, set[ApprovalCandidate]]


class HistoryCache:
    def __init__(
        self, directory: Path, chain_id: int, start_block: int, permit2: str | None, owners: Iterable[str],
    ) -> None:
        self.start_block = start_block
        self.permit2 = Web3.to_checksum_address(permit2) if permit2 else None
        self.owners = tuple(sorted({Web3.to_checksum_address(owner) for owner in owners}))
        scope = json.dumps((_VERSION, chain_id, start_block, self.permit2, self.owners))
        self.path = directory / f"{chain_id}-{sha256(scope.encode()).hexdigest()}.json"

    def load(self) -> HistoryCheckpoint | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            block, block_hash = data["block"], data["block_hash"]
            stored = data["candidates"]
            if (
                type(block) is not int or block < self.start_block
                or not _BLOCK_HASH.fullmatch(block_hash)
                or not isinstance(stored, dict) or set(stored) != set(self.owners)
                or any(not isinstance(entries, list) for entries in stored.values())
            ):
                return None
            candidates = {
                owner: {self._candidate(entry) for entry in entries}
                for owner, entries in stored.items()
            }
            return HistoryCheckpoint(block, block_hash, candidates)
        except (OSError, KeyError, TypeError, ValueError, RecursionError):
            return None

    def _candidate(self, entry: dict) -> ApprovalCandidate:
        candidate = ApprovalCandidate(**entry)
        if not Web3.is_checksum_address(candidate.contract) or not Web3.is_checksum_address(candidate.spender):
            raise ValueError("Invalid address in checkpoint")
        if candidate.kind == "erc721":
            valid = (
                candidate.spender == ZERO_ADDRESS and candidate.token is None
                and type(candidate.token_id) is int and 0 <= candidate.token_id < 2**256
            )
        elif candidate.kind == "permit2":
            valid = (
                candidate.contract == self.permit2 and candidate.token_id is None
                and Web3.is_checksum_address(candidate.token)
            )
        else:
            valid = candidate.kind in ("erc20", "operator") and candidate.token is None and candidate.token_id is None
        if not valid:
            raise ValueError("Invalid approval in checkpoint")
        return candidate

    def save(self, block: int, block_hash: str, candidates: dict[str, set[ApprovalCandidate]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump({
                    "block": block,
                    "block_hash": block_hash,
                    "candidates": {owner: [asdict(candidate) for candidate in candidates[owner]] for owner in self.owners},
                }, file, separators=(",", ":"))
            replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
