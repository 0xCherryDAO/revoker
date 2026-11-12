"""Локальная подпись и последовательная отправка транзакций отзыва."""

from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted


class TransactionFailed(RuntimeError):
    """Транзакция не отправлена или включена в блок со статусом ошибки."""


class TransactionUncertain(RuntimeError):
    """Не удалось подтвердить результат, дальнейшую отправку надо остановить."""


class TransactionSender:
    def __init__(
        self,
        w3: Web3,
        account: LocalAccount,
        chain_id: int,
        receipt_timeout: float = 180,
    ) -> None:
        try:
            actual_chain_id = w3.eth.chain_id
        except Exception:
            raise TransactionFailed("Не удалось получить chain ID от RPC.") from None
        if actual_chain_id != chain_id:
            raise TransactionFailed(
                f"RPC вернул chain ID {actual_chain_id}, ожидался {chain_id}."
            )

        self.eth = w3.eth
        self.account = account
        self.chain_id = chain_id
        self.receipt_timeout = receipt_timeout
        self._next_nonce = 0
        self._in_flight_hash: str | None = None

    def _fees(self) -> tuple[dict[str, int], int]:
        base_fee = self.eth.get_block("latest").get("baseFeePerGas")
        if base_fee is None:
            gas_price = self.eth.gas_price
            return {"gasPrice": gas_price}, gas_price

        try:
            priority_fee = self.eth.max_priority_fee
        except Exception:
            priority_fee = max(0, self.eth.gas_price - base_fee)
        fee_cap = 2 * base_fee + priority_fee
        return {
            "maxFeePerGas": fee_cap,
            "maxPriorityFeePerGas": priority_fee,
        }, fee_cap

    def send(self, to: str, data: bytes | str) -> str:
        if self._in_flight_hash is not None:
            raise TransactionUncertain(
                f"Результат предыдущей транзакции {self._in_flight_hash} неизвестен."
            )

        sender = self.account.address
        call = {"to": to, "data": data, "value": 0}
        try:
            estimate = self.eth.estimate_gas({"from": sender, **call})
        except ContractLogicError:
            raise TransactionFailed("Контракт отклонил симуляцию отзыва.") from None
        except Exception as exc:
            if "insufficient funds" in str(exc).lower():
                raise TransactionFailed("Недостаточно нативной монеты для комиссии.") from None
            raise TransactionFailed("Не удалось оценить газ для отзыва через RPC.") from None

        gas = (estimate * 12 + 9) // 10
        try:
            fees, fee_cap = self._fees()
            nonce = max(self._next_nonce, self.eth.get_transaction_count(sender, "pending"))
            balance = self.eth.get_balance(sender, "pending")
        except Exception:
            raise TransactionFailed("Не удалось получить комиссию, nonce или баланс от RPC.") from None
        if balance < gas * fee_cap:
            raise TransactionFailed("Недостаточно нативной монеты для максимальной комиссии.")

        try:
            signed = self.account.sign_transaction({
                **call,
                **fees,
                "chainId": self.chain_id,
                "gas": gas,
                "nonce": nonce,
            })
        except Exception:
            raise TransactionFailed("Не удалось подписать транзакцию отзыва.") from None

        tx_hash = Web3.to_hex(signed.hash)
        self._in_flight_hash = tx_hash
        try:
            self.eth.send_raw_transaction(signed.raw_transaction)
        except Exception:
            # RPC мог потерять ответ после приёма. Ищем транзакцию по локальному хешу.
            pass

        try:
            receipt = self.eth.wait_for_transaction_receipt(
                signed.hash,
                timeout=self.receipt_timeout,
                poll_latency=2,
            )
        except TimeExhausted:
            raise TransactionUncertain(
                f"Транзакция {tx_hash} не подтверждена за отведённое время."
            ) from None
        except Exception:
            raise TransactionUncertain(
                f"Не удалось получить квитанцию транзакции {tx_hash}."
            ) from None

        status = receipt.get("status")
        if receipt.get("transactionHash") != signed.hash or status not in (0, 1):
            raise TransactionUncertain(f"RPC вернул некорректную квитанцию транзакции {tx_hash}.")

        self._in_flight_hash = None
        self._next_nonce = nonce + 1
        if status == 0:
            raise TransactionFailed(f"Транзакция {tx_hash} завершилась откатом.")
        return tx_hash
