"""Исторические логи из публичного SQD Portal и Envio HyperSync."""

import json
from collections.abc import Iterator
from time import sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from requests import Session
from requests.exceptions import RequestException


SQD_DATASETS = {
    1: "ethereum-mainnet",
    10: "optimism-mainnet",
    56: "binance-mainnet",
    137: "polygon-mainnet",
    8453: "base-mainnet",
    42161: "arbitrum-one",
    43114: "avalanche-mainnet",
}


class IndexUnavailable(RuntimeError):
    """Индексатор не смог предоставить запрошенный диапазон."""


def _query(url: str, token: str, body: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        status = error.code
        error.close()
        raise IndexUnavailable(f"HyperSync вернул HTTP {status}.") from None
    except OSError:
        raise IndexUnavailable("Не удалось подключиться к HyperSync.") from None
    except ValueError:
        raise IndexUnavailable("HyperSync вернул некорректный JSON.") from None
    if not isinstance(result, dict):
        raise IndexUnavailable("HyperSync вернул некорректный ответ.")
    return result


def _normalize_logs(chunks: list) -> list[dict]:
    logs = []
    for chunk in chunks:
        for log in chunk.get("logs", []):
            topics = log.get("topics")
            if topics is None:
                topics = [log.get(f"topic{index}") for index in range(4)]
            if not isinstance(topics, list):
                raise TypeError("Expected a list of log topics")
            while topics and topics[-1] is None:
                topics.pop()
            logs.append({"address": log["address"], "topics": topics, "data": log["data"]})
    return logs


def get_logs(
    chain_id: int,
    start_block: int,
    stop_block: int,
    topics: list[list[str]],
    token: str,
) -> Iterator[tuple[int, list[dict]]]:
    """По каждой странице отдаёт последний покрытый блок включительно и её логи."""
    url = f"https://{chain_id:d}.hypersync.xyz/query"
    body = {
        "from_block": start_block,
        "to_block": stop_block + 1,
        "logs": [{"topics": topics}],
        "field_selection": {"log": ["address", "data", "topic0", "topic1", "topic2", "topic3"]},
        "join_mode": "JoinNothing",
    }

    while body["from_block"] < body["to_block"]:
        page = _query(url, token, body)
        next_block = page.get("next_block")
        archive_height = page.get("archive_height")
        if type(next_block) is not int or next_block > body["to_block"]:
            raise IndexUnavailable("HyperSync вернул некорректный номер следующего блока.")
        if type(archive_height) is int:
            next_block = min(next_block, archive_height + 1)
        if next_block <= body["from_block"]:
            raise IndexUnavailable(f"В HyperSync пока нет блока {body['from_block']}.")
        try:
            logs = _normalize_logs(page["data"])
        except (AttributeError, KeyError, TypeError):
            raise IndexUnavailable("HyperSync вернул некорректные логи.") from None
        yield next_block - 1, logs
        body["from_block"] = next_block


def _sqd_page(session: Session, url: str, body: dict) -> tuple[int, list[dict]]:
    for attempt in range(3):
        try:
            with session.post(url, json=body, timeout=30, stream=True, allow_redirects=False) as response:
                status = response.status_code
                if status == 200:
                    last = body["fromBlock"] - 1
                    logs = []
                    for line in response.iter_lines():
                        if not line.strip():
                            continue
                        block = json.loads(line)
                        number = block["header"]["number"]
                        if type(number) is not int or not last < number <= body["toBlock"]:
                            raise ValueError("Invalid block sequence")
                        last = number
                        logs.extend(block.get("logs", []))
                    return last, logs
                if status == 204:
                    return body["fromBlock"] - 1, []
                retry_after = response.headers.get("Retry-After", "")
        except RequestException:
            raise IndexUnavailable("Не удалось подключиться к SQD Portal.") from None
        except (AttributeError, KeyError, TypeError, ValueError):
            raise IndexUnavailable("SQD Portal вернул некорректные логи.") from None

        delay = int(retry_after) if retry_after.isascii() and retry_after.isdecimal() else 2**attempt
        if status not in (429, 502, 503, 504, 529) or delay > 15 or attempt == 2:
            raise IndexUnavailable(f"SQD Portal вернул HTTP {status}.")
        sleep(delay)


def get_sqd_logs(
    chain_id: int,
    start_block: int,
    stop_block: int,
    topics: list[list[str]],
) -> Iterator[tuple[int, list[dict]]]:
    """Читает финализированную историю из SQD Portal, без API-токена."""
    dataset = SQD_DATASETS.get(chain_id)
    if dataset is None:
        raise IndexUnavailable(f"SQD Portal не настроен для сети {chain_id}.")
    url = f"https://portal.sqd.dev/datasets/{dataset}/finalized-stream"
    body = {
        "type": "evm",
        "fromBlock": start_block,
        "toBlock": stop_block,
        "fields": {"block": {"number": True}, "log": {"address": True, "topics": True, "data": True}},
        "logs": [{f"topic{index}": values for index, values in enumerate(topics) if values}],
    }

    with Session() as session:
        while body["fromBlock"] <= stop_block:
            last, logs = _sqd_page(session, url, body)
            if last < body["fromBlock"]:
                raise IndexUnavailable(f"В SQD Portal пока нет финализированного блока {body['fromBlock']}.")
            yield last, logs
            body["fromBlock"] = last + 1
