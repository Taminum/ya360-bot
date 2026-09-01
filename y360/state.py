"""Состояние бота между перезапусками: выбранная организация и архив счетов."""

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("y360.state")


def data_dir() -> Path:
    path = Path(os.environ.get("DATA_DIR", "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


class State:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else data_dir() / "state.json"
        self._lock = threading.Lock()
        self._data: dict = {"chat_org": {}, "invoices": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data.update(loaded)
            except (ValueError, OSError):
                pass  # битый файл не должен мешать боту стартовать

    def _save(self) -> None:
        try:
            _write_json(self.path, self._data)
        except OSError as exc:
            log.warning("Не сохранил %s: %s", self.path, exc)

    # --- выбранная организация -------------------------------------------
    def chat_org(self, chat_id: int) -> str | None:
        return self._data.get("chat_org", {}).get(str(chat_id))

    def set_chat_org(self, chat_id: int, org_key: str) -> None:
        with self._lock:
            self._data.setdefault("chat_org", {})[str(chat_id)] = org_key
            self._save()

    # --- настройки, заданные из бота ---------------------------------------
    def settings(self, org_key: str) -> dict:
        return dict(self._data.get("settings", {}).get(org_key, {}))

    def set_setting(self, org_key: str, name: str, value) -> None:
        with self._lock:
            self._data.setdefault("settings", {}).setdefault(org_key, {})[name] = value
            self._save()

    # --- мелочи, которые бот помнит ------------------------------------------
    def remember(self, name: str, value) -> None:
        with self._lock:
            self._data.setdefault("memory", {})[name] = value
            self._save()

    def recall(self, name: str, default=None):
        return self._data.get("memory", {}).get(name, default)

    # --- архив счетов ------------------------------------------------------
    def add_invoice(self, org_key: str, record: dict) -> None:
        with self._lock:
            invoices = self._data.setdefault("invoices", {}).setdefault(org_key, [])
            invoices.append(record)
            del invoices[:-200]  # храним последние 200 документов
            self._save()

    def invoices(self, org_key: str, limit: int = 12) -> list[dict]:
        items = self._data.get("invoices", {}).get(org_key, [])
        return list(reversed(items[-limit:]))

    def invoice(self, org_key: str, invoice_id: str) -> dict | None:
        for item in self._data.get("invoices", {}).get(org_key, []):
            if item.get("id") == invoice_id:
                return item
        return None


class PasswordStore:
    """Выданные пароли: чтобы посмотреть их позже, а не выпускать новые.

    Файл лежит рядом с данными бота в открытом виде — включайте хранение
    осознанно (BOT_STORE_PASSWORDS) и берегите доступ к серверу.
    """

    def __init__(self, path: str | Path | None = None, enabled: bool = False):
        self.path = Path(path) if path else data_dir() / "passwords.json"
        self.enabled = enabled
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
            except (ValueError, OSError):
                pass

    @staticmethod
    def _key(org_key: str, nickname: str) -> str:
        return f"{org_key}:{nickname.lower()}"

    def save(self, org_key: str, nickname: str, password: str, **extra) -> bool:
        """True, если пароль действительно записан на диск."""
        if not self.enabled or not nickname:
            return False
        with self._lock:
            self._data[self._key(org_key, nickname)] = {
                "password": password,
                "nickname": nickname,
                **extra,
            }
            try:
                _write_json(self.path, self._data)
                os.chmod(self.path, 0o600)  # только владелец
                return True
            except OSError as exc:
                log.warning("Не сохранил пароль в %s: %s", self.path, exc)
                return False

    def get(self, org_key: str, nickname: str) -> dict | None:
        return self._data.get(self._key(org_key, nickname))

    def all_for(self, org_key: str) -> dict[str, str]:
        prefix = f"{org_key}:"
        return {
            record["nickname"]: record.get("password", "")
            for key, record in self._data.items()
            if key.startswith(prefix)
        }


def _write_json(path: Path, data: dict) -> None:
    """Атомарная запись: сначала во временный файл, потом подмена."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def writable(path: Path | None = None) -> str:
    """Пустая строка, если каталог данных доступен на запись, иначе описание беды."""
    target = Path(path) if path else data_dir()
    probe = target / ".write-test"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return ""
    except OSError as exc:
        return str(exc)
