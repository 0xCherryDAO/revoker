"""Локальный веб-интерфейс для main.py."""

import argparse
from collections import deque
from datetime import datetime
from functools import partial
from hmac import compare_digest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit
import webbrowser

from config import NETWORKS
from src.network_settings import (
    NetworkSettingsConflict, NetworkSettingsError, effective_networks,
    load_settings, save_settings, settings_revision,
)


ROOT = Path(__file__).resolve().parent
NETWORK_SETTINGS_ROUTE = "/api/networks/settings"
STATIC = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/theme.js": ("theme.js", "text/javascript"),
    "/cherry.svg": ("cherry.svg", "image/svg+xml"),
}


def log_level(text: str) -> str:
    message = text.lstrip()
    if message.startswith("[") and "] " in message:
        message = message.split("] ", 1)[1]
    if message.startswith((
        "Не удалось", "Некорректный приватный ключ", "Добавьте приватные ключи",
        "Ожидался chain ID", "Поиск не завершён", "Отзыв не выполнен", "Traceback (most recent call last):",
    )) or (
        "Проверка состояния не удалась" in message
        or "но разрешение осталось" in message
        or message.endswith("Обработка кошелька в этой сети остановлена.")
    ):
        return "ERROR"
    if message.startswith(("RPC:", "Индексатор:", "Остановлено пользователем")):
        return "WARNING"
    if message.startswith(("Отозвано,", "Транзакция обновления nonce Permit2 подтверждена,")):
        return "SUCCESS"
    return "INFO"


def describe_networks(settings: dict, *, detailed: bool = False) -> list[dict]:
    networks = []
    for key, chain in effective_networks(settings).items():
        builtin = NETWORKS.get(key)
        display = "BNB Chain" if key == "bsc" else key.title()
        info = {"id": key, "name": settings.get(key, {}).get("name", display)}
        if detailed:
            info.update(
                builtin=builtin is not None, chain_id=chain.chain_id, rpc=chain.rpc,
                default_rpc=builtin.rpc if builtin is not None else None,
                start_block=chain.start_block, permit2=chain.permit2,
                env_override=f"REVOKER_RPC_{key.upper()}" in os.environ,
            )
        networks.append(info)
    return networks


def settings_snapshot(settings: dict) -> dict:
    return {"settings": settings, "revision": settings_revision(settings), "networks": describe_networks(settings, detailed=True)}


class Runner:
    def __init__(self, stop_file: Path) -> None:
        self.stop_file = stop_file
        self.lock = Lock()
        self.lines = deque(maxlen=2_000)
        self.status = "idle"
        self.run_id = 0
        self.line_id = 0
        self.exit_code = None
        self.dry_run: bool | None = None
        self.started_at: str | None = None
        self.monitor: Thread | None = None
        self.closing = False

    def snapshot(self, after: int = 0) -> dict:
        with self.lock:
            try:
                networks = describe_networks(load_settings())
                network_error = None
            except NetworkSettingsError as exc:
                networks, network_error = [], str(exc)
            return {
                "status": self.status,
                "run_id": self.run_id,
                "lines": [line for line in self.lines if line["id"] > after],
                "networks": networks,
                "network_error": network_error,
                "exit_code": self.exit_code,
                "dry_run": self.dry_run,
                "started_at": self.started_at,
            }

    def network_settings(self) -> dict:
        with self.lock:
            return settings_snapshot(load_settings())

    def update_network_settings(self, settings: dict, revision: str) -> dict:
        with self.lock:
            if self.closing or self.status in {"running", "stopping"}:
                raise RuntimeError("Остановите текущий запуск, чтобы изменить настройки сетей.")
            return settings_snapshot(save_settings(settings, expected_revision=revision))

    def start(self, networks: list[str], dry_run: bool) -> None:
        with self.lock:
            if self.closing or self.status in {"running", "stopping"}:
                raise RuntimeError("Запуск уже выполняется или сервер закрывается.")
            settings = load_settings()
            available = effective_networks(settings)
            if not networks or any(name not in available for name in networks):
                raise NetworkSettingsError("Список сетей изменился. Выберите сети заново.")

            self.stop_file.unlink(missing_ok=True)
            args = [sys.executable, "-u", str(ROOT / "main.py")]
            for network in networks:
                args.extend(("--network", network))
            if dry_run:
                args.append("--dry-run")

            env = {
                **os.environ, "PYTHONIOENCODING": "utf-8", "REVOKER_STOP_FILE": str(self.stop_file),
                "REVOKER_SETTINGS_REVISION": settings_revision(settings),
            }
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            self.lines.clear()
            self.run_id += 1
            self.exit_code = None
            self.dry_run = dry_run
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.status = "running"
            self.monitor = Thread(target=self._collect, args=(process,), name="revoker-output")
            self.monitor.start()

    def _collect(self, process: subprocess.Popen) -> None:
        try:
            with process.stdout as output:
                for line in output:
                    text = line.rstrip("\r\n")
                    with self.lock:
                        self.line_id += 1
                        self.lines.append({
                            "id": self.line_id,
                            "text": text,
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "level": log_level(text),
                        })
        finally:
            exit_code = process.wait()
            with self.lock:
                self.exit_code = exit_code
                self.status = {0: "completed", 130: "stopped"}.get(exit_code, "error")

    def _request_stop(self) -> None:
        if self.status == "running":
            self.stop_file.touch()
            self.status = "stopping"

    def stop(self) -> None:
        with self.lock:
            self._request_stop()

    def close(self) -> None:
        with self.lock:
            self.closing = True
            self._request_stop()
            monitor = self.monitor
        if monitor is not None:
            monitor.join()


class RequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, runner: Runner, token: str, **kwargs) -> None:
        self.runner = runner
        self.authorization = f"Bearer {token}".encode("ascii")
        super().__init__(*args, **kwargs)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def log_message(self, format, *args) -> None:
        pass

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            pass

    def _json(self, data: dict, status: int = HTTPStatus.OK) -> None:
        self._reply(status, "application/json", json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _check_host(self) -> bool:
        if self.headers.get_all("Host") == [self.host] and self.path.startswith("/"):
            return True
        self._json({"error": "Недопустимый адрес сервера."}, HTTPStatus.FORBIDDEN)
        return False

    def _check_auth(self) -> bool:
        values = self.headers.get_all("Authorization", [])
        if len(values) == 1 and compare_digest(values[0].encode("utf-8"), self.authorization):
            return True
        self._json({"error": "Откройте ссылку из окна запуска."}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        if not self._check_host():
            return
        target = urlsplit(self.path)
        if target.path == "/api/status":
            if not self._check_auth():
                return
            try:
                after = int(parse_qs(target.query, max_num_fields=4).get("after", ["0"])[0])
                if after < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "Недопустимый номер строки."}, HTTPStatus.BAD_REQUEST)
                return
            self._json(self.runner.snapshot(after))
            return

        if target.path == NETWORK_SETTINGS_ROUTE:
            if not self._check_auth():
                return
            try:
                self._json(self.runner.network_settings())
            except NetworkSettingsError as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        asset = STATIC.get(target.path)
        if asset is not None:
            filename, mime = asset
            try:
                body = (ROOT / "ui" / filename).read_bytes()
            except OSError:
                self._json({"error": "Файл интерфейса недоступен."}, HTTPStatus.NOT_FOUND)
                return
            self._reply(HTTPStatus.OK, mime, body)
            return
        self._json({"error": "Страница не найдена."}, HTTPStatus.NOT_FOUND)

    def _read_json(self, max_bytes: int = 4_096) -> dict:
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get("Transfer-Encoding") is not None
            or self.headers.get_content_type() != "application/json"
            or len(lengths) != 1
            or not 1 <= int(lengths[0]) <= max_bytes
        ):
            raise ValueError
        value = json.loads(self.rfile.read(int(lengths[0])).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value

    def do_POST(self) -> None:
        if not self._check_host():
            return
        if self.headers.get_all("Origin") != [f"http://{self.host}"]:
            self._json({"error": "Недопустимый источник запроса."}, HTTPStatus.FORBIDDEN)
            return
        if not self._check_auth():
            return
        if self.path not in {"/api/start", "/api/stop", NETWORK_SETTINGS_ROUTE}:
            self._json({"error": "Страница не найдена."}, HTTPStatus.NOT_FOUND)
            return

        try:
            data = self._read_json(max_bytes=4_096 if self.path == "/api/stop" else 512 * 1024)
            if self.path == NETWORK_SETTINGS_ROUTE:
                settings, revision = data.get("settings"), data.get("revision")
                if not isinstance(settings, dict) or not isinstance(revision, str):
                    raise ValueError
                self._json(self.runner.update_network_settings(settings, revision))
                return

            if self.path == "/api/start":
                networks, dry_run = data.get("networks"), data.get("dry_run")
                if (
                    not isinstance(networks, list)
                    or not networks
                    or any(not isinstance(key, str) for key in networks)
                    or len(set(networks)) != len(networks)
                    or not isinstance(dry_run, bool)
                ):
                    raise ValueError
                self.runner.start(networks, dry_run)
            else:
                self.runner.stop()
        except NetworkSettingsConflict as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except NetworkSettingsError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (ValueError, RecursionError, TimeoutError):
            self._json({"error": "Некорректные параметры запроса."}, HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except OSError:
            self._json({"error": "Не удалось запустить или остановить main.py."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json(self.runner.snapshot())


class LocalServer(ThreadingHTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def handle_error(self, request, client_address) -> None:
        print("Не удалось обработать запрос локального интерфейса.", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальный интерфейс отзыва аппрувов")
    parser.add_argument("--no-browser", action="store_true", help="Только напечатать ссылку")
    parser.add_argument("--port", type=int, default=0, help="Порт сервера. 0 выбирает свободный порт")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Порт должен быть числом от 0 до 65535.")

    token = secrets.token_urlsafe(32)
    with TemporaryDirectory(prefix="revoker-") as directory:
        runner = Runner(Path(directory) / "stop")
        handler = partial(RequestHandler, runner=runner, token=token)
        try:
            server = LocalServer(("127.0.0.1", args.port), handler)
        except OSError:
            print("Не удалось запустить локальный сервер. Проверьте доступность порта.", file=sys.stderr)
            return 1

        url = f"http://127.0.0.1:{server.server_port}/#t={token}"
        try:
            with server:
                print(f"Окно программы: {url}", flush=True)
                print("Закрыть программу — Ctrl+C.", flush=True)
                if not args.no_browser:
                    try:
                        webbrowser.open(url)
                    except webbrowser.Error:
                        pass
                server.serve_forever()
        except KeyboardInterrupt:
            print("Завершаю активный запуск.", flush=True)
        finally:
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                runner.close()
            finally:
                signal.signal(signal.SIGINT, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
