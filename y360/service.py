"""Бизнес-логика поверх API: подготовка и создание сотрудника."""

from dataclasses import dataclass, field
import time

from .api import ApiError, Y360Client
from .names import generate_password, make_nickname, translit, unique_nickname

ROOT_DEPARTMENT_ID = 1  # «Все сотрудники»


@dataclass
class NewUser:
    first: str
    last: str
    middle: str = ""
    nickname: str = ""
    password: str = ""
    department_id: int = ROOT_DEPARTMENT_ID
    department_name: str = ""
    position: str = ""
    phone: str = ""
    phone_extension: str = ""
    birthday: str = ""
    gender: str = ""
    about: str = ""
    display_name: str = ""
    external_id: str = ""
    aliases: list[str] = field(default_factory=list)
    password_change_required: bool = True

    def payload(self) -> dict:
        body: dict = {
            "nickname": self.nickname,
            "departmentId": int(self.department_id),
            "password": self.password,
            "passwordChangeRequired": bool(self.password_change_required),
            "name": {"first": self.first, "last": self.last},
        }
        if self.middle:
            body["name"]["middle"] = self.middle
        if self.position:
            body["position"] = self.position
        if self.about:
            body["about"] = self.about
        if self.birthday:
            body["birthday"] = self.birthday
        if self.gender:
            body["gender"] = self.gender
        if self.display_name:
            body["displayName"] = self.display_name
        if self.external_id:
            body["externalId"] = self.external_id
        contacts = []
        if self.phone:
            contacts.append({"type": "phone", "value": self.phone})
        if self.phone_extension:
            contacts.append({"type": "phone_extension", "value": self.phone_extension})
        if contacts:
            body["contacts"] = contacts
        return body


class Directory:
    """Клиент + кэш справочников (отделы, занятые логины)."""

    def __init__(self, client: Y360Client, cache_ttl: int = 300):
        self.client = client
        self.cache_ttl = cache_ttl
        self._departments: list[dict] = []
        self._departments_at = 0.0
        self._nicknames: set[str] = set()
        self._nicknames_at = 0.0
        self._users: list[dict] = []

    # --- справочники ----------------------------------------------------
    def departments(self, refresh: bool = False) -> list[dict]:
        if refresh or not self._departments or time.time() - self._departments_at > self.cache_ttl:
            self._departments = self.client.departments()
            self._departments_at = time.time()
        return self._departments

    def users(self, refresh: bool = False) -> list[dict]:
        if refresh or not self._users or time.time() - self._nicknames_at > self.cache_ttl:
            self._users = self.client.users()
            self._nicknames = set()
            for user in self._users:
                nickname = (user.get("nickname") or "").lower()
                if nickname:
                    self._nicknames.add(nickname)
                for alias in user.get("aliases") or []:
                    self._nicknames.add(str(alias).lower())
                email = (user.get("email") or "").lower()
                if "@" in email:
                    self._nicknames.add(email.split("@", 1)[0])
            self._nicknames_at = time.time()
        return self._users

    def taken_nicknames(self, refresh: bool = False) -> set[str]:
        self.users(refresh=refresh)
        return self._nicknames

    def note_nickname(self, nickname: str) -> None:
        """Отметить логин как занятый, не дожидаясь обновления кэша."""
        self._nicknames.add(nickname.lower())

    def resolve_department(self, value: str | int | None) -> tuple[int, str]:
        """Отдел по id, точному или частичному имени. Пусто -> корневой отдел."""
        if value in (None, ""):
            root = self._department_by_id(ROOT_DEPARTMENT_ID)
            return ROOT_DEPARTMENT_ID, (root or {}).get("name", "")

        text = str(value).strip()
        if text.isdigit():
            found = self._department_by_id(int(text))
            if found:
                return int(found["id"]), found.get("name", "")
            return int(text), ""

        lowered = text.lower()
        departments = self.departments()
        for department in departments:
            if (department.get("name") or "").strip().lower() == lowered:
                return int(department["id"]), department["name"]
        matches = [
            d for d in departments if lowered in (d.get("name") or "").strip().lower()
        ]
        if len(matches) == 1:
            return int(matches[0]["id"]), matches[0]["name"]
        if len(matches) > 1:
            names = ", ".join(m.get("name", "?") for m in matches[:5])
            raise ValueError(f"Отдел «{text}» неоднозначен, подходят: {names}")
        raise ValueError(f"Отдел «{text}» не найден")

    def _department_by_id(self, department_id: int) -> dict | None:
        for department in self.departments():
            if int(department.get("id", 0)) == department_id:
                return department
        return None

    def invalidate(self) -> None:
        """Сбросить кэш сотрудников — после блокировки, удаления и т. п."""
        self._nicknames_at = 0.0

    def user_by_id(self, user_id: str) -> dict | None:
        for user in self._users:
            if str(user.get("id")) == str(user_id):
                return user
        try:
            return self.client.get_user(user_id)
        except Exception:
            return None

    def find_users(self, query: str, limit: int = 10) -> list[dict]:
        lowered = query.strip().lower()
        found = []
        for user in self.users():
            name = user.get("name") or {}
            haystack = " ".join(
                str(part)
                for part in [
                    name.get("last"),
                    name.get("first"),
                    name.get("middle"),
                    user.get("nickname"),
                    user.get("email"),
                    user.get("position"),
                ]
                if part
            ).lower()
            if lowered in haystack:
                found.append(user)
            if len(found) >= limit:
                break
        return found

    # --- подготовка и создание -----------------------------------------
    def prepare(
        self,
        user: NewUser,
        nickname_pattern: str = "{first}.{last}",
        password_length: int = 14,
        refresh: bool = False,
    ) -> NewUser:
        if not user.nickname:
            base = make_nickname(user.first, user.last, nickname_pattern)
            user.nickname = unique_nickname(base, self.taken_nicknames(refresh=refresh))
        else:
            user.nickname = translit(user.nickname) or user.nickname.lower()
        if not user.password:
            user.password = generate_password(password_length)
        if not user.display_name:
            user.display_name = " ".join(x for x in [user.last, user.first] if x)
        return user

    def create(self, user: NewUser) -> dict:
        created = self.client.create_user(user.payload())
        self.note_nickname(user.nickname)
        for alias in user.aliases:
            alias_slug = translit(alias) or alias
            try:
                self.client.add_alias(created["id"], alias_slug)
                self.note_nickname(alias_slug)
            except ApiError as exc:
                created.setdefault("_alias_errors", []).append(f"{alias_slug}: {exc}")
        return created
