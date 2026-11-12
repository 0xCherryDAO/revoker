import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from time import sleep

from hexbytes import HexBytes
from requests.exceptions import ConnectionError, HTTPError, Timeout
from web3 import Web3

from config import PERMIT2_ADDRESS

from .indexed import IndexUnavailable


ZERO_ADDRESS = "0x" + "0" * 40

_APPROVAL = Web3.keccak(text="Approval(address,address,uint256)")
_OPERATOR = Web3.keccak(text="ApprovalForAll(address,address,bool)")
_PERMIT2_APPROVAL = Web3.keccak(text="Approval(address,address,address,uint160,uint48)")
_PERMIT2_PERMIT = Web3.keccak(text="Permit(address,address,address,uint160,uint48,uint48)")
_BLOCK_LIMIT = re.compile(r"\b(?:max(?:imum)?(?: block)? range:?|maximum)\s+([0-9][0-9,]*)")
_RPC_GROW_AFTER = 16


@dataclass(frozen=True)
class ApprovalCandidate:
    kind: str
    contract: str
    spender: str
    token_id: int | None = None
    token: str | None = None


class DiscoveryError(RuntimeError):
    """The RPC could not deliver a complete historical log scan."""


LogSource = Callable[[int, int, list[list[str]]], Iterable[tuple[int, Iterable[Mapping]]]]


def _decode_candidate(
    log: Mapping, topics: list[HexBytes], permit2_address: str | None
) -> ApprovalCandidate | None:
    event = topics[0]
    data = HexBytes(log["data"])
    contract = Web3.to_checksum_address(log["address"])

    if event == _APPROVAL and len(topics) == 3 and len(data) == 32:
        if int.from_bytes(data, "big") > 0:
            return ApprovalCandidate("erc20", contract, Web3.to_checksum_address(topics[2][-20:]))

    if event == _APPROVAL and len(topics) == 4 and not data:
        spender = Web3.to_checksum_address(topics[2][-20:])
        if spender != ZERO_ADDRESS:
            return ApprovalCandidate("erc721", contract, ZERO_ADDRESS, token_id=int.from_bytes(topics[3], "big"))

    if event == _OPERATOR and len(topics) == 3 and len(data) == 32:
        if int.from_bytes(data, "big") == 1:
            return ApprovalCandidate("operator", contract, Web3.to_checksum_address(topics[2][-20:]))

    if contract == permit2_address and len(topics) == 4:
        if (event == _PERMIT2_APPROVAL and len(data) == 64) or (event == _PERMIT2_PERMIT and len(data) == 96):
            token = Web3.to_checksum_address(topics[2][-20:])
            spender = Web3.to_checksum_address(topics[3][-20:])
            return ApprovalCandidate("permit2", contract, spender, token=token)

    return None


def _record_logs(
    logs: Iterable[Mapping],
    candidates: dict[str, set[ApprovalCandidate]],
    owners_by_topic: dict[bytes, str],
    permit2_address: str | None,
    first: int,
    last: int,
) -> None:
    for log in logs:
        try:
            if log.get("removed", False):
                continue
            topics = [HexBytes(topic) for topic in log["topics"]]
            owner = owners_by_topic[topics[1]]
            candidate = _decode_candidate(log, topics, permit2_address)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            raise DiscoveryError(f"Источник вернул некорректный лог в диапазоне {first}–{last}.") from None
        if candidate is not None:
            candidates[owner].add(candidate)


def _request_logs(
    w3: Web3, topics: list[list[str]], first: int, last: int
) -> Sequence[Mapping] | int:
    """Read a block window or return a smaller span when the provider limits it."""
    span = last - first + 1
    for attempt in range(4):
        try:
            logs = w3.eth.get_logs({"topics": topics, "fromBlock": first, "toBlock": last})
        except Exception as error:
            # Provider errors can contain the RPC URL and its API key.
            message = str(error).lower()
            status = None
            if isinstance(error, HTTPError) and error.response is not None:
                status = error.response.status_code
                try:
                    message = str(error.response.json()["error"]).lower()
                except (KeyError, TypeError, ValueError):
                    pass

            if status in (401, 403) or any(
                word in message for word in (
                    "pruned history", "archive requests require", "historical logs unavailable", "unauthorized",
                )
            ):
                raise DiscoveryError(
                    f"RPC не предоставил доступ к логам для блоков {first}–{last}. "
                    "Укажите RPC с доступом к историческим логам."
                ) from None

            rate_limited = status == 429 or any(
                word in message
                for word in ("rate limit", "too many requests", "request limit reached", "compute units per second")
            )
            head_lagging = "beyond current head" in message or "beyond the latest block" in message
            transient_http = status == 408 or status is not None and 500 <= status < 600 and status not in (501, 504, 524)
            if rate_limited or head_lagging or isinstance(error, ConnectionError) or transient_http:
                if attempt < 3:
                    sleep(2**attempt)
                    continue
            elif isinstance(error, Timeout) or status in (413, 504, 524) or any(
                word in message for word in (
                    "block range", "blocks range", "too many blocks", "maximum range", "range is too wide",
                    "range too large", "ranges over", "result limit",
                    "too many results", "query returned more than", "log response size", "response too large",
                    "limit exceeded", "timed out", "timeout",
                )
            ):
                match = _BLOCK_LIMIT.search(message)
                if match is not None:
                    limit = int(match.group(1).replace(",", ""))
                    if 0 < limit < span:
                        return limit
                return span // 2

            reason = f"HTTP {status}" if status is not None else type(error).__name__
            raise DiscoveryError(f"RPC не вернул логи для блоков {first}–{last} ({reason}).") from None

        if not isinstance(logs, (list, tuple)):
            raise DiscoveryError(f"RPC вернул некорректный ответ для блоков {first}–{last}.")
        if len(logs) >= 10_000:
            # Some providers cap results at this size, so try narrower ranges before accepting them.
            return span // 2
        return logs


def _rpc_pages(
    w3: Web3, topics: list[list[str]], first: int, stop: int, max_span: int,
) -> Iterable[tuple[int, Sequence[Mapping]]]:
    span = max_span
    consecutive = 0
    while first <= stop:
        last = min(first + span - 1, stop)
        logs = _request_logs(w3, topics, first, last)
        if isinstance(logs, int):
            if logs == 0:
                raise DiscoveryError(f"RPC не вернул полный список логов для блока {first}.")
            span = logs
            consecutive = 0
            continue
        yield last, logs
        first = last + 1
        consecutive += 1
        if span < max_span and consecutive >= _RPC_GROW_AFTER:
            span = min(span * 2, max_span)
            consecutive = 0


def discover_wallets(
    w3: Web3,
    owners: Iterable[str],
    *,
    from_block: int = 0,
    to_block: int | None = None,
    block_span: int = 50_000,
    permit2_address: str | None = PERMIT2_ADDRESS,
    on_progress: Callable[[int, int], None] | None = None,
    log_source: LogSource | None = None,
    rpc_first: bool = False,
    on_rpc_error: Callable[[str], None] | None = None,
    on_index_error: Callable[[str], None] | None = None,
    initial_candidates: Mapping[str, Iterable[ApprovalCandidate]] | None = None,
    on_checkpoint: Callable[[int, dict[str, set[ApprovalCandidate]]], None] | None = None,
) -> dict[str, set[ApprovalCandidate]]:
    """Collect possible approvals from logs. Read the current state before revoking any candidate."""
    candidates: dict[str, set[ApprovalCandidate]] = {
        Web3.to_checksum_address(owner): set() for owner in owners
    }
    if not candidates:
        return candidates
    if initial_candidates is not None:
        for owner, approvals in candidates.items():
            approvals.update(initial_candidates.get(owner, ()))

    if permit2_address is not None:
        permit2_address = Web3.to_checksum_address(permit2_address)
    signatures = [_APPROVAL, _OPERATOR]
    if permit2_address is not None:
        signatures.extend((_PERMIT2_APPROVAL, _PERMIT2_PERMIT))

    owners_by_topic = {bytes.fromhex(owner[2:]).rjust(32, b"\x00"): owner for owner in candidates}
    topics = [
        [Web3.to_hex(signature) for signature in signatures],
        [Web3.to_hex(topic) for topic in owners_by_topic],
    ]

    if to_block is None:
        try:
            to_block = w3.eth.block_number
        except Exception as error:
            raise DiscoveryError(f"Не удалось получить текущий номер блока ({type(error).__name__}).") from None
    if not (0 <= from_block <= to_block and block_span > 0):
        raise DiscoveryError("Некорректный диапазон блоков для сканирования.")

    if on_progress is not None:
        on_progress(from_block - 1, to_block)
    first = from_block

    def scan(pages: Iterable[tuple[int, Iterable[Mapping]]]) -> None:
        nonlocal first
        for last, logs in pages:
            if not first <= last <= to_block:
                raise DiscoveryError("Источник вернул некорректную границу блоков.")
            _record_logs(logs, candidates, owners_by_topic, permit2_address, first, last)
            first = last + 1
            if on_progress is not None:
                on_progress(last, to_block)
            if on_checkpoint is not None:
                on_checkpoint(last, candidates)

    rpc_error = None
    if rpc_first and log_source is not None:
        try:
            scan(_rpc_pages(w3, topics, first, to_block, block_span))
        except DiscoveryError as error:
            rpc_error = error
            if on_rpc_error is not None:
                on_rpc_error(str(error))
        else:
            return candidates

    if log_source is not None:
        index_start = first
        try:
            scan(log_source(first, to_block, topics))
            if first <= to_block:
                raise DiscoveryError("Индексатор вернул неполный диапазон блоков.")
        except (IndexUnavailable, DiscoveryError) as error:
            resume_rpc = rpc_error is None or first > index_start
            if on_index_error is not None:
                suffix = f" Продолжаю через RPC с блока {first}." if resume_rpc and first <= to_block else ""
                on_index_error(f"{error}{suffix}")
            if not resume_rpc:
                raise rpc_error from None

    scan(_rpc_pages(w3, topics, first, to_block, block_span))
    return candidates
