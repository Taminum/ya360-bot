"""Поиск неиспользуемых ящиков: кто не подавал признаков жизни в Почте.

Оплата в Яндекс 360 идёт за каждого сотрудника, поэтому забытые ящики —
это прямые деньги. Активность считаем по аудит-логу Почты.
"""

from dataclasses import dataclass
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
class Dormant:
    """Сотрудник без активности за период."""

    user: dict
    last_seen: datetime | None
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


def collect_activity(client, days: int, max_pages: int = 500) -> tuple[dict, dict, bool]:
    """Возвращает (активность по логинам, только-входящие, был ли обрыв по лимиту).

    Ключ — логин в нижнем регистре, значение — время последнего действия.
    """
    active: dict[str, datetime] = {}
    passive: dict[str, datetime] = {}
    seen_events = 0

    for event in client.audit_log_mail(iso_days_ago(days), max_pages=max_pages):
        seen_events += 1
        login = str(event.get("userLogin", "")).lower()
        if not login:
            continue
        moment = _parse_date(event.get("date"))
        if not moment:
            continue
        target = active if event.get("eventType") in ACTIVE_EVENTS else passive
        if login not in target or moment > target[login]:
            target[login] = moment

    # упёрлись в лимит страниц — часть событий могла не попасть в выборку
    return active, passive, seen_events >= max_pages * 100


def find_dormant(users: list[dict], active: dict, passive: dict, days: int) -> list[Dormant]:
    """Сотрудники без действий за период. Недавно заведённых не трогаем."""
    threshold = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for user in users:
        if user.get("isRobot"):
            continue
        login = str(user.get("nickname", "")).lower()
        email_login = str(user.get("email", "")).split("@")[0].lower()
        keys = {key for key in (login, email_login) if key}

        last_seen = max(
            (active[key] for key in keys if key in active), default=None
        )
        if last_seen:
            continue  # человек пользуется почтой

        created = _parse_date(user.get("createdAt"))
        if created and created > threshold:
            continue  # завели недавно, ещё не успел

        result.append(
            Dormant(
                user=user,
                last_seen=None,
                received_only=any(key in passive for key in keys),
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
