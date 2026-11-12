import argparse
import sys
from os import environ
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

from src.network_settings import NetworkSettingsError, effective_networks, load_settings, settings_revision
from src.runner import RunStopped, check_stop, run_network


WALLETS_PATH = Path(__file__).resolve().parent / "data" / "wallets.txt"


def load_wallets(path: Path) -> list[LocalAccount]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ValueError("Не удалось прочитать data/wallets.txt в UTF-8.") from None

    wallets = {}
    for number, line in enumerate(lines, start=1):
        key = line.strip()
        if not key:
            continue
        if key[:2].lower() == "0x":
            key = key[2:]
        try:
            if len(key) != 64:
                raise ValueError
            account = Account.from_key(bytes.fromhex(key))
        except Exception:
            raise ValueError(f"Некорректный приватный ключ в строке {number} файла data/wallets.txt.") from None
        wallets[account.address] = account

    if not wallets:
        raise ValueError("Добавьте приватные ключи в data/wallets.txt, по одному на строку.")
    return list(wallets.values())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Отзыв аппрувов кошельков в EVM-сетях")
    try:
        settings = load_settings()
        expected_revision = environ.get("REVOKER_SETTINGS_REVISION")
        if expected_revision is not None and settings_revision(settings) != expected_revision:
            raise NetworkSettingsError("Настройки сетей изменились. Запустите проверку или отзыв заново.")
        available_networks = effective_networks(settings)
    except NetworkSettingsError as exc:
        parser.error(str(exc))
    parser.add_argument("--dry-run", action="store_true", help="Показать разрешения без отправки транзакций")
    parser.add_argument("--network", action="append", choices=available_networks, help="Выбрать сеть. Можно указать несколько раз")
    args = parser.parse_args()

    try:
        check_stop()
        wallets = load_wallets(WALLETS_PATH)
    except ValueError as exc:
        print(exc)
        return 2

    networks = tuple(dict.fromkeys(args.network or available_networks))
    if args.dry_run:
        print("Режим просмотра. Транзакции не отправляются.")
    print(f"Кошельков {len(wallets)}, сетей {len(networks)}.", flush=True)

    errors = 0
    for name in networks:
        errors += run_network(name, available_networks[name], wallets, dry_run=args.dry_run)
        check_stop()
    return int(errors > 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RunStopped:
        print("\nОстановлено пользователем.")
        sys.exit(130)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем. Уже отправленные транзакции могут быть исполнены.")
        sys.exit(130)
