import json
import os
import re
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from web3 import Web3

from config import NETWORKS, Network


SETTINGS_PATH = Path(__file__).resolve().parents[1] / "run" / "network-settings.json"
_NETWORK_ID = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_CUSTOM_REQUIRED = {"name", "chain_id", "rpc"}
_CUSTOM_FIELDS = _CUSTOM_REQUIRED | {"start_block", "permit2"}


class NetworkSettingsError(ValueError):
    """The local network settings are invalid or could not be accessed."""


class NetworkSettingsConflict(NetworkSettingsError):
    """The file changed after the caller loaded the settings."""


def _rpc_url(value: object) -> str:
    message = "Введите корректный RPC URL с протоколом HTTP или HTTPS."
    if not isinstance(value, str):
        raise NetworkSettingsError(message)
    url = value.strip()
    if not url or "\\" in url or "#" in url or any(not char.isprintable() or char.isspace() for char in url):
        raise NetworkSettingsError(message)
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0:
            return url
    except ValueError:
        pass
    raise NetworkSettingsError(message)


def _permit2_address(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        address = value.strip()
        if not address:
            return None
        if _ADDRESS.fullmatch(address) and int(address, 16):
            return Web3.to_checksum_address(address)
    raise NetworkSettingsError("Укажите действительный адрес контракта Permit2 или оставьте поле пустым.")


def _normalize_settings(settings: object) -> dict[str, dict]:
    if not isinstance(settings, dict):
        raise NetworkSettingsError("Ожидается список настроек сетей.")

    normalized = {}
    chain_ids = {network.chain_id for network in NETWORKS.values()}
    for network_id, entry in settings.items():
        if not isinstance(network_id, str) or len(network_id) > 40 or not _NETWORK_ID.fullmatch(network_id):
            raise NetworkSettingsError("Идентификатор сети должен состоять из строчных латинских букв, цифр и подчёркиваний.")
        if not isinstance(entry, dict):
            raise NetworkSettingsError("Некорректные настройки сети.")
        if network_id in NETWORKS:
            if entry.keys() != {"rpc"}:
                raise NetworkSettingsError("Для встроенной сети можно изменить только RPC.")
            normalized[network_id] = {"rpc": _rpc_url(entry["rpc"])}
            continue

        if not _CUSTOM_REQUIRED <= entry.keys() <= _CUSTOM_FIELDS:
            raise NetworkSettingsError("Для своей сети укажите название, chain ID и RPC URL.")
        name = entry["name"].strip() if isinstance(entry["name"], str) else ""
        if not name or not name.isprintable():
            raise NetworkSettingsError("Введите название сети.")
        chain_id = entry["chain_id"]
        if type(chain_id) is not int or not 1 <= chain_id <= _MAX_SAFE_INTEGER:
            raise NetworkSettingsError("Chain ID должен быть положительным целым числом.")
        if chain_id in chain_ids:
            raise NetworkSettingsError("Сеть с таким chain ID уже есть в списке.")
        start_block = entry.get("start_block", 0)
        if type(start_block) is not int or not 0 <= start_block <= _MAX_SAFE_INTEGER:
            raise NetworkSettingsError("Начальный блок должен быть неотрицательным целым числом.")

        normalized[network_id] = {
            "name": name,
            "chain_id": chain_id,
            "rpc": _rpc_url(entry["rpc"]),
            "start_block": start_block,
            "permit2": _permit2_address(entry.get("permit2")),
        }
        chain_ids.add(chain_id)
    return normalized


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise NetworkSettingsError("В файле настроек повторяется одно и то же поле.")
    return result


def load_settings() -> dict[str, dict]:
    try:
        raw = SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise NetworkSettingsError("Не удалось прочитать файл настроек сетей.") from None

    try:
        return _normalize_settings(json.loads(raw, object_pairs_hook=_unique_keys))
    except (ValueError, RecursionError):
        raise NetworkSettingsError("Файл настроек сетей повреждён или содержит неверные значения.") from None


def settings_revision(settings: object) -> str:
    canonical = json.dumps(_normalize_settings(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


@contextmanager
def _write_lock():
    with SETTINGS_PATH.with_suffix(".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def save_settings(settings: object, *, expected_revision: str | None = None) -> dict[str, dict]:
    normalized = _normalize_settings(settings)
    temporary = None
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock():
            current = load_settings()
            if expected_revision is not None and expected_revision != settings_revision(current):
                raise NetworkSettingsConflict("Настройки уже изменились. Обновите их перед сохранением.")

            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(normalized, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temporary, SETTINGS_PATH)
    except OSError:
        raise NetworkSettingsError("Не удалось сохранить настройки сетей.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return normalized


def effective_networks(settings: object | None = None) -> dict[str, Network]:
    stored = load_settings() if settings is None else _normalize_settings(settings)
    networks = dict(NETWORKS)
    for network_id, entry in stored.items():
        if network_id in networks:
            networks[network_id] = replace(networks[network_id], rpc=entry["rpc"])
        else:
            networks[network_id] = Network(
                chain_id=entry["chain_id"], rpc=entry["rpc"],
                start_block=entry["start_block"], permit2=entry["permit2"],
            )
    return networks
