"""Состояние бота между перезапусками: выбранная организация и архив счетов."""

import json
import os
import threading
from pathlib import Path


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # --- выбранная организация -------------------------------------------
    def chat_org(self, chat_id: int) -> str | None:
        return self._data.get("chat_org", {}).get(str(chat_id))

    def set_chat_org(self, chat_id: int, org_key: str) -> None:
        with self._lock:
            self._data.setdefault("chat_org", {})[str(chat_id)] = org_key
            self._save()

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
