"""Поиск неиспользуемых ящиков: кто не подавал признаков жизни в Почте.

Оплата в Яндекс 360 идёт за каждого сотрудника, поэтому забытые ящики —
это прямые деньги. Активность считаем по аудит-логу Почты.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Действия, которые совершает сам сотрудник.
# message_receive намеренно не в списке: письмо приходит само, даже в мёртвый ящик.
ACTIVE_EVENTS = {
    "mailbox_send",
    "message_seen",
    "message_unseen",
    "message_forward",
    "message_purge",
    "message_trash",
    "message_spam",
    "message_unspam",
    "message_move",
    "message_copy",
    "message_answer",
}


@dataclass
class ActivityScan:
    """Что удалось вычитать из аудит-лога."""

    active: dict[str, datetime] = field(default_factory=dict)
    passive: dict[str, datetime] = field(default_factory=dict)
    events: int = 0
    truncated: bool = False
    samples: list[dict] = field(default_factory=list)
    mode: str = ""            # какой вариант запроса сработал
    failures: list[str] = field(default_factory=list)  # что не сработало

    @property
    def people(self) -> int:
        return len(self.active)


@dataclass
class Dormant:
    """Сотрудник без активности за период."""

    user: dict
    received_only: bool  # письма приходили, но человек их не открывал

    @property
    def email(self) -> str:
        return str(self.user.get("email", ""))

    @property
    def full_name(self) -> str:
        name = self.user.get("name") or {}
        return " ".join(
            str(part)
            for part in [name.get("last"), name.get("first"), name.get("middle")]
            if part
        ) or str(self.user.get("nickname", ""))

    @property
    def enabled(self) -> bool:
        return bool(self.user.get("isEnabled", True))

    @property
    def created(self) -> str:
        return str(self.user.get("createdAt", ""))[:10]


def iso_days_ago(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def event_keys(event: dict) -> set[str]:
    """Все обозначения человека в событии: uid, логин, адрес без домена."""
    keys = set()
    uid = str(event.get("userUid") or "").strip()
    if uid:
        keys.add(uid)
    login = str(event.get("userLogin") or "").strip().lower()
    if login:
        keys.add(login)
        keys.add(login.split("@")[0])
    return keys


def user_keys(user: dict) -> set[str]:
    """Те же обозначения со стороны справочника сотрудников."""
    keys = set()
    uid = str(user.get("id") or "").strip()
    if uid:
        keys.add(uid)
    nickname = str(user.get("nickname") or "").strip().lower()
    if nickname:
        keys.add(nickname)
    email = str(user.get("email") or "").strip().lower()
    if email:
        keys.add(email)
        keys.add(email.split("@")[0])
    for alias in user.get("aliases") or []:
        alias = str(alias).strip().lower()
        if alias:
            keys.add(alias)
            keys.add(alias.split("@")[0])
    return keys


def query_variants(days: int) -> list[tuple[str, dict]]:
    """Разные написания одного и того же запроса.

    Аудит-лог отвечает HTTP 500 на неудобные ему параметры, поэтому
    подбираем рабочий вариант, а не гадаем.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    after = now - timedelta(days=days)
    z_form = after.isoformat().replace("+00:00", "Z")
    offset_form = after.isoformat()

    return [
        ("afterDate с Z", {"pageSize": 100, "afterDate": z_form}),
        ("afterDate с +00:00", {"pageSize": 100, "afterDate": offset_form}),
        (
            "afterDate и beforeDate",
            {
                "pageSize": 100,
                "afterDate": z_form,
                "beforeDate": now.isoformat().replace("+00:00", "Z"),
            },
        ),
        ("только дата без времени", {"pageSize": 100, "afterDate": after.date().isoformat()}),
        ("страница по 50", {"pageSize": 50, "afterDate": z_form}),
        ("без периода", {"pageSize": 100}),
    ]


def probe_query(client, days: int) -> tuple[dict, str, list[str]]:
    """Возвращает (рабочие параметры, их название, список неудач)."""
    failures = []
    for label, params in query_variants(days):
        try:
            client.request(
                "GET", client.audit_mail_path, params=dict(params, pageSize=1), retries=1
            )
            return params, label, failures
        except Exception as exc:  # noqa: BLE001 — нам важен сам факт отказа
            failures.append(f"{label}: {_short(exc)}")
    raise AuditUnavailable(failures)


class AuditUnavailable(RuntimeError):
    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__("Аудит-лог Почты не отвечает ни на один вариант запроса")


def collect_activity(client, days: int, max_pages: int = 500) -> ActivityScan:
    """Читает аудит-лог Почты и раскладывает события по людям."""
    scan = ActivityScan()
    params, label, failures = probe_query(client, days)
    scan.mode = label
    scan.failures = failures

    for event in client.audit_log_mail(params, max_pages=max_pages):
        scan.events += 1
        if len(scan.samples) < 5:
            scan.samples.append({
                "userUid": event.get("userUid"),
                "userLogin": event.get("userLogin"),
                "eventType": event.get("eventType"),
                "date": event.get("date"),
            })
        moment = _parse_date(event.get("date"))
        if not moment:
            continue
        target = scan.active if event.get("eventType") in ACTIVE_EVENTS else scan.passive
        for key in event_keys(event):
            if key not in target or moment > target[key]:
                target[key] = moment

    # упёрлись в лимит страниц — часть событий могла не попасть в выборку
    scan.truncated = scan.events >= max_pages * 100
    return scan


def find_dormant(users: list[dict], scan: ActivityScan, days: int) -> list[Dormant]:
    """Сотрудники без действий за период. Недавно заведённых не трогаем."""
    threshold = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for user in users:
        if user.get("isRobot"):
            continue
        keys = user_keys(user)
        if any(key in scan.active for key in keys):
            continue  # человек пользуется почтой

        created = _parse_date(user.get("createdAt"))
        if created and created > threshold:
            continue  # завели недавно, ещё не успел

        result.append(
            Dormant(
                user=user,
                received_only=any(key in scan.passive for key in keys),
            )
        )
    return sorted(result, key=lambda item: (item.enabled, item.created))


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _short(exc: Exception, limit: int = 90) -> str:
    text = str(exc)
    marker = " -> "
    if marker in text:
        text = text.split(marker, 1)[1]
    return text[:limit]
