"""Реестр организаций: одна или несколько, у каждой свой токен и настройки."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .api import Y360Client
from .names import translit
from .service import Directory

DEFAULT_CONFIG = "orgs.json"


@dataclass
class Org:
    key: str
    name: str
    org_id: str
    token: str = field(repr=False, default="")
    domain: str = ""
    default_department: str = ""
    nickname_pattern: str = "{first}.{last}"
    translit: str = "passport"
    password_length: int = 14
    password_change_required: bool = True
    allow_delete: bool = False

    @property
    def title(self) -> str:
        return self.name or self.key or self.org_id

    @property
    def admin_url(self) -> str:
        return "https://admin.yandex.ru"

    @property
    def billing_url(self) -> str:
        return "https://admin.yandex.ru/billing"


class OrgRegistry:
    """Организации из orgs.json, либо одна из переменных окружения."""

    def __init__(self, orgs: list[Org]):
        if not orgs:
            raise SystemExit(
                "Не настроена ни одна организация: создайте orgs.json "
                "(см. orgs.example.json) или задайте Y360_TOKEN и Y360_ORG_ID в .env"
            )
        self.orgs = orgs
        self._by_key = {org.key: org for org in orgs}
        self._directories: dict[str, Directory] = {}

    # --- загрузка -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "OrgRegistry":
        candidates = [path] if path else [
            os.environ.get("Y360_ORGS_FILE"),
            Path("config") / DEFAULT_CONFIG,
            DEFAULT_CONFIG,
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return cls(cls._from_file(Path(candidate)))
        return cls(cls._from_env())

    @staticmethod
    def _from_file(path: Path) -> list[Org]:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        items = raw.get("orgs", raw) if isinstance(raw, dict) else raw
        orgs = []
        for index, item in enumerate(items, start=1):
            name = str(item.get("name") or item.get("org_id") or f"org{index}")
            key = str(item.get("key") or translit(name) or f"org{index}")
            token = str(item.get("token") or "")
            if token.startswith("$"):  # можно держать токен в переменной окружения
                token = os.environ.get(token[1:], "")
            if not token:
                raise SystemExit(f"У организации «{name}» в {path} не задан token")
            orgs.append(
                Org(
                    key=key,
                    name=name,
                    org_id=str(item["org_id"]),
                    token=token,
                    domain=str(item.get("domain", "")),
                    default_department=str(item.get("default_department", "")),
                    nickname_pattern=str(
                        item.get("nickname_pattern", "{first}.{last}")
                    ),
                    translit=str(item.get("translit", "passport")),
                    password_length=int(item.get("password_length", 14)),
                    password_change_required=bool(
                        item.get("password_change_required", True)
                    ),
                    allow_delete=bool(item.get("allow_delete", False)),
                )
            )
        return orgs

    @staticmethod
    def _from_env() -> list[Org]:
        token = os.environ.get("Y360_TOKEN", "")
        org_id = os.environ.get("Y360_ORG_ID", "")
        if not token or not org_id:
            return []
        return [
            Org(
                key="default",
                name=os.environ.get("Y360_ORG_NAME", "Организация"),
                org_id=org_id,
                token=token,
                domain=os.environ.get("Y360_DOMAIN", ""),
                default_department=os.environ.get("BOT_DEFAULT_DEPARTMENT", ""),
                nickname_pattern=os.environ.get(
                    "Y360_NICKNAME_PATTERN", "{first}.{last}"
                ),
                translit=os.environ.get("Y360_TRANSLIT", "passport"),
                password_length=int(os.environ.get("Y360_PASSWORD_LENGTH", "14")),
                password_change_required=_flag("Y360_PASSWORD_CHANGE_REQUIRED", True),
                allow_delete=_flag("BOT_ALLOW_DELETE", False),
            )
        ]

    # --- доступ ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self.orgs)

    def __iter__(self):
        return iter(self.orgs)

    @property
    def default(self) -> Org:
        return self.orgs[0]

    def get(self, key: str | None) -> Org:
        if not key:
            return self.default
        return self._by_key.get(key, self.default)

    def has(self, key: str | None) -> bool:
        return key in self._by_key

    def directory(self, key: str | None = None) -> Directory:
        org = self.get(key)
        if org.key not in self._directories:
            self._directories[org.key] = Directory(
                Y360Client(org.token, org.org_id)
            )
        return self._directories[org.key]


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "да", "on"}
