"""Минимальный клиент Bot API (long polling), без внешних зависимостей кроме requests."""

import logging
import time
from typing import Any, Iterator

import requests

log = logging.getLogger("y360.telegram")


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, timeout: int = 25):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout
        self.session = requests.Session()
        self.offset: int | None = None

    def call(self, method: str, **params) -> Any:
        url = f"{self.base}/{method}"
        for attempt in range(1, 6):
            try:
                response = self.session.post(
                    url, json=params, timeout=self.timeout + 10
                )
            except requests.RequestException as exc:
                log.warning("Telegram: сетевая ошибка (%s/5): %s", attempt, exc)
                time.sleep(min(2 ** attempt, 30))
                continue

            data = response.json()
            if data.get("ok"):
                return data.get("result")

            if response.status_code == 429:
                wait = data.get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram: 429, ждём %s c", wait)
                time.sleep(wait)
                continue
            raise TelegramError(f"{method}: {data.get('description')}")
        raise TelegramError(f"{method}: не удалось выполнить запрос")

    def poll(self, long_poll_timeout: int = 25) -> Iterator[dict]:
        """Бесконечный поток обновлений."""
        while True:
            params: dict[str, Any] = {
                "timeout": long_poll_timeout,
                "allowed_updates": ["message", "callback_query"],
            }
            if self.offset is not None:
                params["offset"] = self.offset
            try:
                updates = self.call("getUpdates", **params) or []
            except TelegramError as exc:
                log.error("Ошибка получения обновлений: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                self.offset = update["update_id"] + 1
                yield update

    # --- сахар ----------------------------------------------------------
    def send(self, chat_id: int, text: str, **kwargs) -> dict:
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            **kwargs,
        )

    def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> Any:
        return self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            **kwargs,
        )

    def answer_callback(self, callback_id: str, text: str = "") -> Any:
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def delete(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            log.debug("Не удалось удалить сообщение: %s", exc)

    def send_document(
        self, chat_id: int, file_id: str, caption: str = "", **kwargs
    ) -> dict:
        """Отправляет уже известный боту файл по file_id — без повторной загрузки."""
        params = {"chat_id": chat_id, "document": file_id}
        if caption:
            params["caption"] = caption
            params["parse_mode"] = "HTML"
        return self.call("sendDocument", **params, **kwargs)

    def upload_document(
        self, chat_id: int, path: str, caption: str = ""
    ) -> dict:
        """Заливает локальный файл (запасной путь, если file_id протух)."""
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        with open(path, "rb") as handle:
            response = self.session.post(
                f"{self.base}/sendDocument",
                data=data,
                files={"document": handle},
                timeout=120,
            )
        payload = response.json()
        if not payload.get("ok"):
            raise TelegramError(f"sendDocument: {payload.get('description')}")
        return payload["result"]

    def download_file(self, file_id: str, destination) -> None:
        """Скачивает файл, присланный в чат, на диск."""
        from pathlib import Path as _Path

        info = self.call("getFile", file_id=file_id)
        remote_path = info["file_path"]
        url = self.base.replace("/bot", "/file/bot") + "/" + remote_path
        with self.session.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            destination = _Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    handle.write(chunk)

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        try:
            self.call(
                "setMyCommands",
                commands=[{"command": c, "description": d} for c, d in commands],
            )
        except TelegramError as exc:
            log.debug("setMyCommands: %s", exc)
