from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from os import environ
from pathlib import Path
from time import monotonic

from eth_account.signers.local import LocalAccount
from web3 import HTTPProvider, Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import NETWORKS, Network, RECEIPT_TIMEOUT, RPC_TIMEOUT, rpc_url
from src.approvals import MAX_PERMIT2_NONCE, ApprovalState, permit2_invalidation_data, read_approval, revocation_data
from src.discovery import ApprovalCandidate, DiscoveryError, LogSource, discover_wallets
from src.history_cache import HistoryCache
from src.indexed import SQD_DATASETS, get_logs, get_sqd_logs
from src.transactions import TransactionFailed, TransactionSender, TransactionUncertain


HISTORY_DIR = Path(__file__).resolve().parents[1] / "run" / "approval-history"
CHECKPOINT_INTERVAL = 15


@dataclass
class WalletResult:
    found: int = 0
    revoked: int = 0
    errors: int = 0


class RunStopped(Exception):
    pass


def check_stop() -> None:
    marker = environ.get("REVOKER_STOP_FILE")
    if marker and Path(marker).exists():
        raise RunStopped


def describe(candidate: ApprovalCandidate, state: ApprovalState) -> str:
    if candidate.kind == "erc721":
        return f"ERC-721 {candidate.contract} #{candidate.token_id} → {state.spender}"
    if candidate.kind == "operator":
        return f"NFT оператор {candidate.contract} → {state.spender}"
    if candidate.kind == "permit2":
        return f"Permit2 {candidate.token} → {state.spender} (лимит {state.amount})"
    return f"ERC-20 {candidate.contract} → {state.spender} (лимит {state.amount})"


def revoke_wallet(
    w3: Web3,
    chain: Network,
    account: LocalAccount,
    candidates: set[ApprovalCandidate],
    *,
    dry_run: bool,
) -> WalletResult:
    result = WalletResult()
    sender = None
    # Cut the underlying ERC-20 access first, then clear the Permit2 sub-allowances.
    priority = {"operator": 1, "erc20": 2, "erc721": 3, "permit2": 4}
    permit2 = (chain.permit2 or "").lower()
    ordered = sorted(candidates, key=lambda item: (
        0 if item.kind == "erc20" and item.spender.lower() == permit2 else priority[item.kind],
        item.contract, item.token or "", item.token_id or 0, item.spender,
    ))
    for candidate in ordered:
        check_stop()
        try:
            state = read_approval(w3, account.address, candidate)
        except Exception as exc:
            print(f"  Не удалось проверить {candidate.kind} {candidate.contract} ({type(exc).__name__}).")
            result.errors += 1
            continue
        if not state.amount:
            continue

        result.found += 1
        print(f"  {describe(candidate, state)}", flush=True)
        if dry_run:
            continue

        updated_nonce = None
        try:
            if sender is None:
                sender = TransactionSender(w3, account, chain.chain_id, receipt_timeout=RECEIPT_TIMEOUT)
            if state.nonce is not None:
                try:
                    if state.nonce == MAX_PERMIT2_NONCE:
                        raise TransactionFailed("Достигнут максимальный nonce Permit2.")
                    tx_hash = sender.send(candidate.contract, permit2_invalidation_data(w3, candidate, state.nonce))
                    updated_nonce = state.nonce + 1
                    print(f"  Транзакция обновления nonce Permit2 подтверждена, {tx_hash}", flush=True)
                except TransactionFailed as exc:
                    print(f"  Не удалось обновить nonce Permit2. {exc}")
                    result.errors += 1

            tx_hash = sender.send(candidate.contract, revocation_data(w3, candidate))
        except TransactionUncertain as exc:
            print(f"  {exc} Обработка кошелька в этой сети остановлена.")
            result.errors += 1
            break
        except TransactionFailed as exc:
            print(f"  Отзыв не выполнен. {exc}")
            result.errors += 1
            continue

        try:
            final_state = read_approval(w3, account.address, candidate)
        except Exception as exc:
            print(f"  Транзакция {tx_hash} подтверждена. Проверка состояния не удалась ({type(exc).__name__}).")
            result.errors += 1
            continue
        if updated_nonce is not None and (final_state.nonce is None or final_state.nonce < updated_nonce):
            print("  Не удалось подтвердить изменение nonce Permit2 по состоянию контракта.")
            result.errors += 1
        if final_state.amount:
            print(f"  Транзакция {tx_hash} подтверждена, но разрешение осталось.")
            result.errors += 1
            continue
        result.revoked += 1
        print(f"  Отозвано, {tx_hash}", flush=True)
    return result


def _discover_network(
    w3: Web3, name: str, chain: Network, owners: list[str], on_progress: Callable[[int, int], None],
    *, source: str, log_source: LogSource | None,
) -> dict[str, set[ApprovalCandidate]]:
    try:
        head = w3.eth.block_number
    except Exception as error:
        raise DiscoveryError(f"Не удалось получить текущий номер блока ({type(error).__name__}).") from None

    def report_progress(block: int, _stop: int) -> None:
        on_progress(block, head)

    scan = partial(
        discover_wallets, w3, owners, block_span=chain.block_span,
        permit2_address=chain.permit2, on_progress=report_progress,
    )
    scan_history = partial(
        scan, log_source=log_source, rpc_first=source == "RPC",
        on_rpc_error=lambda message: print(f"[{name}] RPC: {message} Пробую SQD Portal.", flush=True),
        on_index_error=lambda message: print(f"[{name}] Индексатор: {message}", flush=True),
    )
    try:
        finalized = w3.eth.get_block("finalized")
        stable_block = finalized["number"]
        stable_hash = Web3.to_hex(finalized["hash"])
    except Exception:
        stable_block = -1
    if type(stable_block) is not int or not chain.start_block <= stable_block <= head:
        return scan_history(from_block=chain.start_block, to_block=head)

    cache = HistoryCache(HISTORY_DIR, chain.chain_id, chain.start_block, chain.permit2, owners)
    checkpoint = cache.load()
    first = chain.start_block
    candidates = {}
    if checkpoint is not None and checkpoint.block <= stable_block:
        try:
            anchor = stable_hash if checkpoint.block == stable_block else Web3.to_hex(w3.eth.get_block(checkpoint.block)["hash"])
        except Exception:
            anchor = None
        if anchor == checkpoint.block_hash:
            first = checkpoint.block + 1
            candidates = checkpoint.candidates
            print(f"[{name}] Кеш истории до блока {checkpoint.block:,}. Продолжаю с блока {first:,}.", flush=True)

    last_saved = monotonic()

    def save_checkpoint(block: int, found: dict[str, set[ApprovalCandidate]]) -> None:
        nonlocal last_saved
        now = monotonic()
        if block != stable_block and now - last_saved < CHECKPOINT_INTERVAL:
            return
        last_saved = now
        try:
            block_hash = stable_hash if block == stable_block else Web3.to_hex(w3.eth.get_block(block)["hash"])
            cache.save(block, block_hash, found)
        except Exception:
            # A local cache or anchor read failure does not invalidate the scan.
            print(f"[{name}] Кеш истории не удалось сохранить для блока {block:,}.", flush=True)

    if first <= stable_block:
        candidates = scan_history(
            from_block=first, to_block=stable_block, initial_candidates=candidates, on_checkpoint=save_checkpoint,
        )
    if stable_block < head:
        candidates = scan(from_block=stable_block + 1, to_block=head, initial_candidates=candidates)
    return candidates


def run_network(name: str, chain: Network, wallets: list[LocalAccount], *, dry_run: bool) -> int:
    check_stop()
    endpoint = rpc_url(name, chain)
    token = environ.get("ENVIO_API_TOKEN")
    builtin = NETWORKS.get(name)
    log_source = None
    source = "RPC"
    if builtin is not None and token:
        log_source = partial(get_logs, chain.chain_id, token=token)
        source = "HyperSync"
    elif builtin is not None and endpoint == builtin.rpc and chain.chain_id in SQD_DATASETS:
        log_source = partial(get_sqd_logs, chain.chain_id)
        if chain.index_first:
            source = "SQD Portal"

    print(f"\n[{name}] Поиск аппрувов через {source} с блока {chain.start_block}", flush=True)
    w3 = Web3(HTTPProvider(
        endpoint, request_kwargs={"timeout": RPC_TIMEOUT}, exception_retry_configuration=None,
    ))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    try:
        actual_chain_id = w3.eth.chain_id
    except Exception as exc:
        print(f"[{name}] Не удалось подключиться к RPC ({type(exc).__name__}).")
        return 1
    if actual_chain_id != chain.chain_id:
        print(f"[{name}] Ожидался chain ID {chain.chain_id}, RPC вернул {actual_chain_id}.")
        return 1

    next_progress = 10
    last_report = monotonic()

    def on_progress(block: int, head: int) -> None:
        nonlocal next_progress, last_report
        check_stop()
        if chain.start_block <= block <= head:
            percent = (block - chain.start_block + 1) * 100 // (head - chain.start_block + 1)
            now = monotonic()
            if percent >= next_progress or now - last_report >= 15:
                print(f"[{name}] Проверено {percent}% истории, блок {block:,}", flush=True)
                next_progress = (percent // 10 + 1) * 10
                last_report = now

    try:
        approvals = _discover_network(
            w3, name, chain, [wallet.address for wallet in wallets], on_progress,
            source=source, log_source=log_source,
        )
    except DiscoveryError as exc:
        print(f"[{name}] Поиск не завершён. {exc}")
        return 1

    errors = 0
    for wallet in wallets:
        check_stop()
        print(f"[{name}] {wallet.address}", flush=True)
        result = revoke_wallet(w3, chain, wallet, approvals[wallet.address], dry_run=dry_run)
        errors += result.errors
        summary = f"Ненулевых разрешений {result.found}"
        if not dry_run:
            summary += f", отозвано {result.revoked}"
        print(f"  {summary}, ошибок {result.errors}.", flush=True)
    check_stop()
    return errors
