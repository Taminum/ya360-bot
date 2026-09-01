#!/usr/bin/env python3
"""Телеграм-бот администратора Яндекс 360.

Заводит сотрудников («Фамилия Имя» → почта и пароль), ищет их, блокирует,
сбрасывает пароли, удаляет и хранит архив счетов. Умеет несколько организаций.

Запуск:  python bot.py   (или docker compose up -d)
"""

import html
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from y360.activity import collect_activity, find_dormant, user_keys
from y360.api import ApiError
from y360.config import load_env, require
from y360.csvio import read_rows, write_rows
from y360.names import generate_password
from y360.orgs import Org, OrgRegistry
from y360.service import Directory, NewUser
from y360.state import PasswordStore, State, data_dir, writable
from y360.telegram import TelegramClient, TelegramError
from y360 import xlsx

log = logging.getLogger("y360.bot")

HELP = """<b>Что умею</b>

<b>Завести сотрудника</b> — просто отправьте ФИО:
<code>Иванов Иван</code>
<code>Иванов Иван Иванович</code>
<code>Иванов Иван | Отдел продаж | Менеджер | +79990000001</code>
Логин задаётся вручную через <code>@</code>:
<code>Иванов Иван | @i.ivanov</code>

<b>Команды</b>
/find Иванов — карточка сотрудника: блокировка, сброс пароля, удаление
/dept — отделы с их ID
/export — все сотрудники таблицей
/last — кого заводили последними
/dead — ящики без активности (период в /settings, разово: /dead 90)
/journal — весь журнал файлом
/backup — прислать копию данных бота
/settings — длина пароля, шаблон логина и прочее
/org — выбрать организацию
/bill — оплата и архив счетов
/id — ваш Telegram ID
/help — эта справка

Счёт на оплату можно просто прислать сюда файлом — сохраню в архив организации."""

AUDIT_FIELDS = [
    "at", "org", "action", "last", "first", "middle", "nickname", "email",
    "password", "department", "position", "telegram_user", "status", "error",
]

_VAULT_WARNING = (
    "\n⚠️ Пароль не сохранён в архив: нет прав на запись в data/.\n"
    "На сервере: <code>sudo chown -R 10001:10001 data</code>"
)

MENU = {
    "🔍 Найти": "find",
    "📊 Выгрузка": "export",
    "🕘 Последние": "last",
    "💤 Неактивные": "dead",
    "⚙️ Настройки": "settings",
    "💳 Счета": "bill",
    "🏢 Отделы": "dept",
}

KEYBOARD = {
    "keyboard": [
        [{"text": "🔍 Найти"}, {"text": "📊 Выгрузка"}],
        [{"text": "🕘 Последние"}, {"text": "🏢 Отделы"}],
        [{"text": "💤 Неактивные"}, {"text": "💳 Счета"}],
        [{"text": "⚙️ Настройки"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

SETTINGS_CHOICES = {
    "password_length": ("Длина пароля", [
        ("10 символов", 10), ("12 символов", 12), ("14 символов", 14),
        ("16 символов", 16), ("20 символов", 20),
    ]),
    "password_change_required": ("Смена пароля при первом входе", [
        ("требовать", True), ("не требовать", False),
    ]),
    "nickname_pattern": ("Шаблон логина", [
        ("i.ivanov", "{f}.{last}"), ("ivan.ivanov", "{first}.{last}"),
        ("iivanov", "{f}{last}"), ("ivanov", "{last}"),
    ]),
    "translit": ("Транслитерация", [
        ("Барысхан -> baryshan", "simple"), ("Барысхан -> baryskhan", "passport"),
    ]),
    "name_order": ("Порядок слов в ФИО", [
        ("Фамилия Имя", "last_first"), ("Имя Фамилия", "first_last"),
    ]),
    "dormant_days": ("Период поиска неактивных", [
        ("30 дней", 30), ("60 дней", 60), ("90 дней", 90), ("180 дней", 180),
    ]),
    "auto_delete": ("Удалять пароль из чата", [
        ("не удалять", 0), ("через 1 минуту", 60),
        ("через 5 минут", 300), ("через 15 минут", 900),
    ]),
}

# «m.ivanov» или «m.ivanov@company.ru» — это поиск, а не новый сотрудник
LOOKS_LIKE_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9.-]+)?$")

DELETE_PHRASE = re.compile(r"^\s*удалить\s+(\S+)\s*$", re.IGNORECASE)


class Bot:
    def __init__(self):
        self.telegram = TelegramClient(
            require("TELEGRAM_BOT_TOKEN"),
            proxy=os.environ.get("TELEGRAM_PROXY") or None,
        )
        self.registry = OrgRegistry.load()
        self.state = State()
        self.allowed = self._parse_allowed(os.environ.get("TELEGRAM_ALLOWED_USERS", ""))
        self.auto_delete = int(os.environ.get("BOT_AUTO_DELETE_SECONDS", "0"))
        self.name_order = (
            os.environ.get("BOT_NAME_ORDER", "last_first").strip().lower()
        )
        self.log_passwords = _flag("BOT_LOG_PASSWORDS", False)
        self.vault = PasswordStore(
            os.environ.get("BOT_PASSWORDS_FILE") or None,
            enabled=_flag("BOT_STORE_PASSWORDS", False),
        )
        self.audit_path = Path(
            os.environ.get("BOT_AUDIT_FILE", str(data_dir() / "journal.csv"))
        )
        self.backup_days = int(os.environ.get("BOT_BACKUP_DAYS", "0") or 0)
        self.dormant_days = int(os.environ.get("BOT_DORMANT_DAYS", "30") or 30)
        self.invoices_dir = Path(
            os.environ.get("BOT_INVOICES_DIR", str(data_dir() / "invoices"))
        )
        self.pending: dict[str, dict] = {}
        self.welcome: dict[str, dict] = {}
        self.pending_delete: dict[int, dict] = {}
        self.to_delete: list[tuple[int, int, float]] = []
        self._counter = 0

    @staticmethod
    def _parse_allowed(raw: str) -> set[int]:
        ids = {int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()}
        if not ids:
            raise SystemExit(
                "TELEGRAM_ALLOWED_USERS пуст. Бот выдаёт пароли и умеет удалять "
                "сотрудников — без белого списка он не запускается.\n"
                "Свой ID узнайте у @userinfobot и впишите в .env."
            )
        return ids

    # --- организации -----------------------------------------------------
    def org_for(self, chat_id: int) -> Org:
        key = self.state.chat_org(chat_id)
        return self.registry.get(key if self.registry.has(key) else None)

    def directory_for(self, chat_id: int) -> Directory:
        return self.registry.directory(self.org_for(chat_id).key)

    def setting(self, org: Org, name: str):
        """Значение настройки: сначала заданное из бота, потом из .env."""
        overrides = self.state.settings(org.key)
        if name in overrides:
            return overrides[name]
        return {
            "password_length": org.password_length,
            "nickname_pattern": org.nickname_pattern,
            "translit": org.translit,
            "password_change_required": org.password_change_required,
            "name_order": self.name_order,
            "auto_delete": self.auto_delete,
            "dormant_days": self.dormant_days,
        }[name]

    def delay_for(self, chat_id: int) -> int:
        return int(self.setting(self.org_for(chat_id), "auto_delete") or 0)

    def help_text(self) -> str:
        """Примеры в справке — в том порядке ФИО, который настроен."""
        if self.name_order == "first_last":
            return HELP.replace("Иванов Иван Иванович", "Иван Иванов Иванович").replace(
                "Иванов Иван", "Иван Иванов"
            )
        return HELP

    def org_prefix(self, chat_id: int) -> str:
        if len(self.registry) < 2:
            return ""
        return f"<i>{html.escape(self.org_for(chat_id).title)}</i>\n"

    # --- цикл -------------------------------------------------------------
    def run(self) -> None:
        problem = writable()
        if problem:
            log.error(
                "Каталог данных недоступен для записи (%s). Журнал, пароли и архив "
                "счетов сохраняться не будут. На сервере выполните: "
                "sudo chown -R 10001:10001 data", problem,
            )
        me = self.telegram.call("getMe")
        log.info(
            "Бот @%s запущен. Организаций: %s, доступ у %s пользователей",
            me.get("username"), len(self.registry), len(self.allowed),
        )
        self.telegram.set_commands([
            ("find", "Найти сотрудника"),
            ("dept", "Список отделов"),
            ("export", "Выгрузить всех в таблицу"),
            ("last", "Последние заведённые"),
            ("dead", "Неиспользуемые ящики"),
            ("settings", "Настройки"),
            ("journal", "Журнал файлом"),
            ("backup", "Копия данных"),
            ("org", "Выбрать организацию"),
            ("bill", "Оплата и счета"),
            ("help", "Справка"),
            ("id", "Ваш Telegram ID"),
        ])
        for update in self.telegram.poll():
            try:
                self.handle(update)
            except Exception as exc:  # одно кривое сообщение не должно ронять бота
                log.exception("Ошибка обработки обновления")
                self._report_error(update, exc)
            self._process_deletions()
            self.maybe_auto_backup()

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

    # --- входящие сообщения ------------------------------------------------
    def on_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        sender = message.get("from") or {}
        text = (message.get("text") or "").strip()

        if text.startswith("/id") or text.startswith("/start"):
            self.cmd_start(chat_id, sender)
            return

        if not self._authorized(chat_id, sender):
            return

        self.state.remember("last_chat", chat_id)

        if message.get("document"):
            self.save_invoice(chat_id, sender, message)
            return
        if not text:
            return

        if text in MENU:  # нажата кнопка нижней клавиатуры
            text = "/" + MENU[text]

        if text.startswith("/help"):
            self.telegram.send(
                chat_id, self.org_prefix(chat_id) + self.help_text(),
                reply_markup=KEYBOARD,
            )
        elif text.startswith("/org"):
            self.cmd_orgs(chat_id)
        elif text.startswith("/dept"):
            self.cmd_departments(chat_id)
        elif text.startswith("/export"):
            self.cmd_export(chat_id)
        elif text.startswith("/settings") or text.startswith("/set"):
            self.cmd_settings(chat_id)
        elif text.startswith("/last"):
            self.cmd_last(chat_id)
        elif text.startswith("/dead") or text.startswith("/inactive"):
            self.cmd_dormant(chat_id, text.split(" ", 1)[-1] if " " in text else "")
        elif text.startswith("/find"):
            self.cmd_find(chat_id, text[len("/find"):].strip())
        elif text.startswith("/bill"):
            self.cmd_bill(chat_id)
        elif text.startswith("/backup"):
            self.cmd_backup(chat_id)
        elif text.startswith("/journal"):
            self.cmd_journal(chat_id)
        elif text.startswith("/"):
            self.telegram.send(chat_id, "Неизвестная команда. /help")
        elif DELETE_PHRASE.match(text):
            self.confirm_delete(chat_id, sender, DELETE_PHRASE.match(text).group(1))
        elif len(text.split()) == 1:
            # одно слово — это поиск: и «m.ivanov», и «Иванов»
            self.cmd_find(chat_id, text.split("@")[0])
        else:
            self.propose_user(chat_id, text)

    def _authorized(self, chat_id: int, sender: dict) -> bool:
        if sender.get("id") in self.allowed:
            return True
        log.warning(
            "Отказано пользователю %s (@%s)", sender.get("id"), sender.get("username")
        )
        self.telegram.send(
            chat_id,
            "Нет доступа. Передайте администратору свой ID: "
            f"<code>{sender.get('id')}</code>",
        )
        return False

    # --- простые команды ----------------------------------------------------
    def cmd_start(self, chat_id: int, sender: dict) -> None:
        is_allowed = sender.get("id") in self.allowed
        body = (
            f"Ваш Telegram ID: <code>{sender.get('id')}</code>\n"
            f"Доступ: <b>{'есть' if is_allowed else 'нет'}</b>"
        )
        if is_allowed:
            body = self.org_prefix(chat_id) + body + "\n\n" + HELP
        self.telegram.send(chat_id, body)

    def cmd_orgs(self, chat_id: int) -> None:
        current = self.org_for(chat_id)
        if len(self.registry) < 2:
            self.telegram.send(
                chat_id,
                f"Организация: <b>{html.escape(current.title)}</b> "
                f"(ID <code>{current.org_id}</code>).\n"
                "Чтобы вести несколько — опишите их в <code>orgs.json</code>.",
            )
            return
        buttons = [
            [{
                "text": ("✅ " if org.key == current.key else "") + org.title,
                "callback_data": f"org:{org.key}",
            }]
            for org in self.registry
        ]
        self.telegram.send(
            chat_id,
            f"Текущая организация: <b>{html.escape(current.title)}</b>\nПереключить:",
            reply_markup={"inline_keyboard": buttons},
        )

    def cmd_departments(self, chat_id: int) -> None:
        try:
            departments = self.directory_for(chat_id).departments(refresh=True)
        except ApiError as exc:
            self.telegram.send(chat_id, _api_error(exc))
            return
        lines = [
            f"<code>{d.get('id')}</code> — {html.escape(str(d.get('name', '')))}"
            f" ({d.get('membersCount', 0)})"
            for d in sorted(departments, key=lambda x: int(x.get("id", 0)))
        ]
        self.telegram.send(
            chat_id, self.org_prefix(chat_id) + "<b>Отделы</b>\n" + "\n".join(lines[:60])
        )

    # --- поиск и карточка сотрудника -----------------------------------------
    def cmd_find(self, chat_id: int, query: str) -> None:
        if not query:
            self.telegram.send(
                chat_id,
                "Отправьте фамилию, логин или почту — покажу карточку.",
            )
            return
        try:
            found = self.directory_for(chat_id).find_users(query, limit=12)
        except ApiError as exc:
            self.telegram.send(chat_id, _api_error(exc))
            return
        if not found:
            self.telegram.send(chat_id, "Никого не нашёл.")
            return
        if len(found) == 1:
            self.show_card(chat_id, str(found[0]["id"]))
            return
        buttons = [
            [{
                "text": f"{_full_name(user)} — {user.get('email', '')}"[:64],
                "callback_data": f"u:{user['id']}",
            }]
            for user in found
        ]
        self.telegram.send(
            chat_id,
            self.org_prefix(chat_id) + f"Нашёл {len(found)}. Выберите:",
            reply_markup={"inline_keyboard": buttons},
        )

    def card_text(self, chat_id: int, user: dict) -> str:
        name = user.get("name") or {}
        phones = [
            c.get("value") for c in user.get("contacts") or []
            if c.get("type") == "phone" and not c.get("synthetic")
        ]
        enabled = user.get("isEnabled", True)
        lines = [
            self.org_prefix(chat_id) + f"<b>{html.escape(_full_name(user))}</b>",
            f"Почта: <code>{html.escape(str(user.get('email', '')))}</code>",
        ]
        aliases = user.get("aliases") or []
        if aliases:
            lines.append("Алиасы: " + ", ".join(html.escape(str(a)) for a in aliases))
        if user.get("position"):
            lines.append(f"Должность: {html.escape(str(user['position']))}")
        if phones:
            lines.append(f"Телефон: {html.escape(str(phones[0]))}")
        lines.append(f"Отдел ID: <code>{user.get('departmentId', '')}</code>")
        lines.append(
            "Статус: " + ("🟢 активен" if enabled else "🔴 заблокирован")
            + (" · админ" if user.get("isAdmin") else "")
        )
        if user.get("createdAt"):
            lines.append(f"Создан: {str(user['createdAt'])[:10]}")
        _ = name
        return "\n".join(lines)

    def card_buttons(self, chat_id: int, user: dict) -> dict:
        user_id = user["id"]
        enabled = user.get("isEnabled", True)
        rows = [[
            {
                "text": "🔒 Заблокировать" if enabled else "🔓 Разблокировать",
                "callback_data": f"blk:{user_id}:{0 if enabled else 1}",
            },
            {"text": "🔑 Новый пароль", "callback_data": f"pwd:{user_id}"},
        ]]
        if self.vault.enabled:
            rows.append(
                [{"text": "🔐 Выданный пароль", "callback_data": f"pv:{user_id}"}]
            )
        if self.org_for(chat_id).allow_delete:
            rows.append([{"text": "🗑 Удалить", "callback_data": f"del:{user_id}"}])
        return {"inline_keyboard": rows}

    def show_card(
        self, chat_id: int, user_id: str, message_id: int | None = None
    ) -> None:
        directory = self.directory_for(chat_id)
        user = directory.user_by_id(user_id)
        if not user:
            self.telegram.send(chat_id, "Сотрудник не найден — возможно, уже удалён.")
            return
        text = self.card_text(chat_id, user)
        markup = self.card_buttons(chat_id, user)
        if message_id:
            self.telegram.edit(chat_id, message_id, text, reply_markup=markup)
        else:
            self.telegram.send(chat_id, text, reply_markup=markup)

    # --- создание сотрудника --------------------------------------------------
    def propose_user(self, chat_id: int, text: str) -> None:
        org = self.org_for(chat_id)
        directory = self.directory_for(chat_id)
        try:
            new_user = self.parse_input(text, self.setting(org, "name_order"))
        except ValueError as exc:
            self.telegram.send(chat_id, f"{html.escape(str(exc))}\n\n{HELP}")
            return

        # отдел не указан — берём тот же, что и в прошлый раз
        asked = new_user.department_name
        remembered = "" if asked else str(
            self.state.recall(f"department:{org.key}", "") or ""
        )
        try:
            department_id, department_name = directory.resolve_department(
                asked or remembered or org.default_department
            )
        except ValueError as exc:
            self.telegram.send(
                chat_id, f"{html.escape(str(exc))}\nСписок отделов: /dept"
            )
            return
        except ApiError as exc:
            self.telegram.send(chat_id, _api_error(exc))
            return

        new_user.department_id = department_id
        new_user.department_name = department_name
        new_user.password_change_required = bool(
            self.setting(org, "password_change_required")
        )

        try:
            directory.prepare(
                new_user,
                nickname_pattern=self.setting(org, "nickname_pattern"),
                password_length=int(self.setting(org, "password_length")),
                translit_style=self.setting(org, "translit"),
            )
        except ApiError as exc:
            self.telegram.send(chat_id, _api_error(exc))
            return

        self._counter += 1
        key = str(self._counter)
        self.pending[key] = {
            "user": new_user,
            "org": org.key,
            "created_at": time.time(),
        }
        self._gc_pending()

        preview = [
            self.org_prefix(chat_id)
            + f"<b>{html.escape(_join(new_user.last, new_user.first, new_user.middle))}</b>",
            f"Логин: <code>{html.escape(new_user.nickname)}</code>"
            + (f"@{html.escape(org.domain)}" if org.domain else ""),
            f"Отдел: {html.escape(new_user.department_name or str(department_id))}"
            + ("  <i>(как в прошлый раз)</i>" if remembered and not asked else ""),
        ]
        if new_user.position:
            preview.append(f"Должность: {html.escape(new_user.position)}")
        if new_user.phone:
            preview.append(f"Телефон: {html.escape(new_user.phone)}")

        twins = self.find_twins(directory, new_user)
        if twins:
            preview.append("")
            preview.append("⚠️ <b>Такой сотрудник уже есть:</b>")
            for twin in twins[:3]:
                status = "" if twin.get("isEnabled", True) else " (заблокирован)"
                preview.append(
                    f"• {html.escape(_full_name(twin))} — "
                    f"<code>{html.escape(str(twin.get('email', '')))}</code>{status}"
                )
        preview.append("\nСоздать сотрудника?")

        self.telegram.send(
            chat_id,
            "\n".join(preview),
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Создать", "callback_data": f"mk:{key}"},
                {"text": "✖️ Отмена", "callback_data": f"mkno:{key}"},
            ]]},
        )

    def do_create(self, chat_id: int, message_id: int, sender: dict, key: str) -> None:
        entry = self.pending.pop(key, None)
        if not entry:
            self.telegram.edit(
                chat_id, message_id, "Заявка устарела — отправьте ФИО заново."
            )
            return

        org = self.registry.get(entry["org"])
        directory = self.registry.directory(org.key)
        new_user: NewUser = entry["user"]
        self.telegram.edit(chat_id, message_id, "⏳ Создаю сотрудника…")

        try:
            created = directory.create(new_user)
        except ApiError as exc:
            self._audit(org, "create", new_user, "", sender, "error", str(exc))
            self.telegram.edit(chat_id, message_id, "❌ Не создан.\n" + _api_error(exc))
            return

        email = created.get("email") or new_user.nickname
        lines = [
            f"✅ <b>{html.escape(_join(new_user.last, new_user.first, new_user.middle))}</b>",
            "",
            f"Логин: <code>{html.escape(email)}</code>",
            f"Пароль: <code>{html.escape(new_user.password)}</code>",
            "Вход через яндекс почту",
        ]
        for error in created.get("_alias_errors", []):
            lines.append(f"⚠️ алиас не добавлен — {html.escape(error)}")
        delay = self.delay_for(chat_id)
        if delay:
            lines.append(f"<i>Сообщение удалится через {delay} с.</i>")

        saved = self.vault.save(
            org.key, new_user.nickname, new_user.password,
            email=email, at=_now(), by=_who(sender), action="create",
        )
        if self.vault.enabled and not saved:
            lines.append(_VAULT_WARNING)

        # текст для пересылки сотруднику готовим заранее — пароль больше нигде не хранится
        self._counter += 1
        welcome_key = f"w{self._counter}"
        self.welcome[welcome_key] = {
            "email": email,
            "password": new_user.password,
            "name": _join(new_user.first, new_user.middle),
            "change": bool(new_user.password_change_required),
            "at": time.time(),
        }

        buttons = [[{
            "text": "📋 Текст для сотрудника",
            "callback_data": f"wel:{welcome_key}",
        }]]
        if org.allow_delete and created.get("id"):
            # опечатку в ФИО видно сразу — не заставляем идти через /find
            buttons.append(
                [{"text": "🗑 Удалить", "callback_data": f"del:{created['id']}"}]
            )

        self.telegram.edit(
            chat_id, message_id, "\n".join(lines),
            reply_markup={"inline_keyboard": buttons},
        )
        self.state.remember(
            f"department:{org.key}",
            new_user.department_name or new_user.department_id,
        )
        self._audit(org, "create", new_user, email, sender, "ok", "")
        if delay:
            self.to_delete.append((chat_id, message_id, time.time() + delay))

    # --- блокировка, пароль, удаление -------------------------------------------
    def do_block(
        self, chat_id: int, message_id: int, sender: dict, user_id: str, enable: bool
    ) -> None:
        org = self.org_for(chat_id)
        directory = self.directory_for(chat_id)
        user = directory.user_by_id(user_id) or {}
        try:
            directory.client.set_enabled(user_id, enable)
        except ApiError as exc:
            self._audit_user(org, "unblock" if enable else "block", user, sender,
                             "error", str(exc))
            self.telegram.send(chat_id, _api_error(exc))
            return
        directory.invalidate()
        self._audit_user(org, "unblock" if enable else "block", user, sender, "ok", "")
        self.telegram.send(
            chat_id,
            ("🔓 Разблокирован: " if enable else "🔒 Заблокирован: ")
            + f"<code>{html.escape(str(user.get('email', user_id)))}</code>"
            + ("" if enable else "\n<i>Почта и файлы сохранены, вход закрыт.</i>"),
        )
        self.show_card(chat_id, user_id, message_id)

    def do_password(
        self, chat_id: int, message_id: int, sender: dict, user_id: str
    ) -> None:
        org = self.org_for(chat_id)
        directory = self.directory_for(chat_id)
        user = directory.user_by_id(user_id) or {}
        password = generate_password(int(self.setting(org, "password_length")))
        try:
            directory.client.set_password(
                user_id, password, bool(self.setting(org, "password_change_required"))
            )
        except ApiError as exc:
            self._audit_user(org, "password", user, sender, "error", str(exc))
            self.telegram.send(chat_id, _api_error(exc))
            return

        saved = self.vault.save(
            org.key, str(user.get("nickname", "")), password,
            email=str(user.get("email", "")), at=_now(), by=_who(sender),
            action="reset",
        )
        self._audit_user(
            org, "password", user, sender, "ok", "",
            password=password if self.log_passwords else "",
        )
        lines = [
            f"🔑 <b>{html.escape(_full_name(user))}</b>",
            f"Почта: <code>{html.escape(str(user.get('email', '')))}</code>",
            f"Новый пароль: <code>{html.escape(password)}</code>",
        ]
        if self.setting(org, "password_change_required"):
            lines.append("<i>Сменить при первом входе.</i>")
        if self.vault.enabled and not saved:
            lines.append(_VAULT_WARNING)
        sent = self.telegram.send(chat_id, "\n".join(lines))
        delay = self.delay_for(chat_id)
        if delay:
            self.to_delete.append(
                (chat_id, sent["message_id"], time.time() + delay)
            )
        _ = message_id

    def show_saved_password(self, chat_id: int, user_id: str) -> None:
        org = self.org_for(chat_id)
        user = self.directory_for(chat_id).user_by_id(user_id) or {}
        nickname = str(user.get("nickname", ""))
        record = self.vault.get(org.key, nickname)
        if not record:
            self.telegram.send(
                chat_id,
                "Пароль не сохранён — сотрудник заведён до включения хранения "
                "или менял пароль сам.\nВыдайте новый кнопкой «🔑 Новый пароль».",
            )
            return
        issued = str(record.get("at", ""))[:16].replace("T", " ")
        lines = [
            f"🔐 <b>{html.escape(_full_name(user))}</b>",
            f"Почта: <code>{html.escape(str(user.get('email', '')))}</code>",
            f"Пароль: <code>{html.escape(record.get('password', ''))}</code>",
            f"<i>выдан {issued}</i>",
        ]
        sent = self.telegram.send(chat_id, "\n".join(lines))
        delay = self.delay_for(chat_id)
        if delay:
            self.to_delete.append(
                (chat_id, sent["message_id"], time.time() + delay)
            )

    def cmd_export(self, chat_id: int) -> None:
        org = self.org_for(chat_id)
        directory = self.directory_for(chat_id)
        notice = self.telegram.send(chat_id, "⏳ Собираю выгрузку…")

        try:
            users = directory.users(refresh=True)
            departments = {
                int(item.get("id", 0)): str(item.get("name", ""))
                for item in directory.departments(refresh=True)
            }
        except ApiError as exc:
            self.telegram.edit(chat_id, notice["message_id"], _api_error(exc))
            return

        passwords = self.vault.all_for(org.key) if self.vault.enabled else {}
        fields = [
            "last", "first", "middle", "nickname", "email", "aliases",
            "department", "position", "phone", "is_enabled", "created_at",
        ]
        if passwords:
            fields.insert(6, "password")

        rows = []
        for user in sorted(users, key=lambda u: _full_name(u).lower()):
            name = user.get("name") or {}
            phones = [
                c.get("value") for c in user.get("contacts") or []
                if c.get("type") == "phone" and not c.get("synthetic")
            ]
            nickname = str(user.get("nickname", ""))
            rows.append({
                "last": name.get("last", ""),
                "first": name.get("first", ""),
                "middle": name.get("middle", ""),
                "nickname": nickname,
                "email": user.get("email", ""),
                "password": passwords.get(nickname, ""),
                "aliases": " ".join(user.get("aliases") or []),
                "department": departments.get(int(user.get("departmentId", 0) or 0), ""),
                "position": user.get("position", ""),
                "phone": phones[0] if phones else "",
                "is_enabled": "да" if user.get("isEnabled", True) else "нет",
                "created_at": str(user.get("createdAt", ""))[:10],
            })

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        directory_path = data_dir() / "exports"
        try:
            if xlsx.available():
                path = xlsx.write_users(
                    directory_path / f"{org.key}_{stamp}.xlsx", rows, fields
                )
            else:
                path = write_rows(
                    directory_path / f"{org.key}_{stamp}.csv", rows, fields
                )
        except Exception as exc:
            log.exception("Не удалось собрать выгрузку")
            self.telegram.edit(
                chat_id, notice["message_id"],
                f"Не удалось собрать файл: <code>{html.escape(str(exc))}</code>",
            )
            return

        caption = f"Сотрудники «{org.title}» — {len(rows)}"
        if passwords:
            caption += ", включая выданные пароли"
        self.telegram.upload_document(chat_id, str(path), caption)
        self.telegram.delete(chat_id, notice["message_id"])

    @staticmethod
    def find_twins(directory: Directory, new_user: NewUser) -> list[dict]:
        """Однофамильцы с тем же именем — чтобы не завести человека дважды."""
        first = new_user.first.strip().lower()
        last = new_user.last.strip().lower()
        twins = []
        for user in directory.users():
            name = user.get("name") or {}
            if (
                str(name.get("first", "")).strip().lower() == first
                and str(name.get("last", "")).strip().lower() == last
            ):
                twins.append(user)
        return twins

    # --- настройки прямо в боте --------------------------------------------
    def cmd_settings(self, chat_id: int, message_id: int | None = None) -> None:
        org = self.org_for(chat_id)
        lines = [self.org_prefix(chat_id) + "<b>Настройки</b>", ""]
        buttons = []
        for field, (title, choices) in SETTINGS_CHOICES.items():
            current = self.setting(org, field)
            label = next(
                (name for name, value in choices if value == current), str(current)
            )
            lines.append(f"{title}: <b>{html.escape(label)}</b>")
            buttons.append([{"text": title, "callback_data": f"cfg:{field}"}])
        lines.append("")
        lines.append("<i>Действуют для текущей организации и переживают перезапуск.</i>")

        markup = {"inline_keyboard": buttons}
        text = "\n".join(lines)
        if message_id:
            self.telegram.edit(chat_id, message_id, text, reply_markup=markup)
        else:
            self.telegram.send(chat_id, text, reply_markup=markup)

    def show_setting_choices(self, chat_id: int, message_id: int, field: str) -> None:
        if field not in SETTINGS_CHOICES:
            return
        org = self.org_for(chat_id)
        title, choices = SETTINGS_CHOICES[field]
        current = self.setting(org, field)
        buttons = [
            [{
                "text": ("✅ " if value == current else "") + name,
                "callback_data": f"cfgs:{field}:{index}",
            }]
            for index, (name, value) in enumerate(choices)
        ]
        buttons.append([{"text": "‹ Назад", "callback_data": "cfg:back"}])
        self.telegram.edit(
            chat_id, message_id, f"<b>{title}</b>",
            reply_markup={"inline_keyboard": buttons},
        )

    def apply_setting(
        self, chat_id: int, message_id: int, field: str, index: str
    ) -> None:
        if field not in SETTINGS_CHOICES or not index.isdigit():
            return
        _, choices = SETTINGS_CHOICES[field]
        position = int(index)
        if position >= len(choices):
            return
        self.state.set_setting(self.org_for(chat_id).key, field, choices[position][1])
        self.cmd_settings(chat_id, message_id)

    # --- готовый текст для сотрудника ---------------------------------------
    def send_welcome(self, chat_id: int, key: str) -> None:
        entry = self.welcome.get(key)
        if not entry:
            self.telegram.send(
                chat_id, "Текст устарел — пароль можно выдать заново в карточке."
            )
            return
        greeting = f"Здравствуйте, {entry['name']}!" if entry["name"] else "Здравствуйте!"
        lines = [
            greeting,
            "",
            "Ваша рабочая почта готова.",
            f"Логин: <code>{html.escape(entry['email'])}</code>",
            f"Пароль: <code>{html.escape(entry['password'])}</code>",
            "",
            "Вход через Яндекс Почту: https://mail.yandex.ru",
        ]
        if entry["change"]:
            lines.append("При первом входе система попросит сменить пароль.")
        self.telegram.send(chat_id, "\n".join(lines))

    # --- неиспользуемые ящики --------------------------------------------------
    def cmd_dormant(self, chat_id: int, argument: str = "") -> None:
        org = self.org_for(chat_id)
        directory = self.directory_for(chat_id)
        debug = argument.strip().lower() in {"debug", "отладка", "?"}
        days = (
            int(argument) if argument.strip().isdigit()
            else int(self.setting(org, "dormant_days"))
        )
        days = max(7, min(days, 365))

        notice = self.telegram.send(
            chat_id,
            f"⏳ Смотрю журнал Почты за {days} дн. Это может занять минуту…",
        )
        try:
            users = directory.users(refresh=True)
            scan = collect_activity(directory.client, days)
        except ApiError as exc:
            self.telegram.edit(
                chat_id, notice["message_id"],
                _api_error(exc) + (
                    "\n\n<i>Для этой команды приложению нужно право "
                    "<code>ya360_security:audit_log_mail</code>. Добавьте его на "
                    "oauth.yandex.ru и выпустите токен заново.</i>"
                    if exc.status in (401, 403) else ""
                ),
            )
            return

        if debug:
            self._send_dormant_debug(chat_id, notice["message_id"], users, scan, days)
            return

        dormant = find_dormant(users, scan, days)
        blocked = [item for item in dormant if not item.enabled]
        idle = [item for item in dormant if item.enabled]

        lines = [
            self.org_prefix(chat_id) + f"<b>Неиспользуемые ящики за {days} дн.</b>",
            f"Всего сотрудников: {len(users)} · без активности: {len(dormant)}",
            f"<i>Событий в журнале: {scan.events}, из них активных людей: "
            f"{self._matched(users, scan)} из {len(users)}</i>",
            "",
        ]
        if idle:
            lines.append(f"<b>Активны, но не пользуются ({len(idle)})</b>")
            for item in idle[:20]:
                mark = " · письма приходят" if item.received_only else ""
                lines.append(
                    f"• {html.escape(item.full_name)} — "
                    f"<code>{html.escape(item.email)}</code>{mark}"
                )
        if blocked:
            lines.append("")
            lines.append(f"<b>Заблокированы ({len(blocked)})</b> — платите за них зря")
            for item in blocked[:20]:
                lines.append(
                    f"• {html.escape(item.full_name)} — "
                    f"<code>{html.escape(item.email)}</code>"
                )
        if not dormant:
            lines.append("Все пользуются почтой — лишних ящиков нет.")
        if len(dormant) > 20:
            lines.append("")
            lines.append("<i>Показаны первые 20, полный список — в файле.</i>")
        if scan.truncated:
            lines.append("")
            lines.append(
                "<i>⚠️ Журнал очень большой, просмотрена только часть. "
                "Возьмите период поменьше, например /dead 30.</i>"
            )
        lines.append("")
        lines.append(
            "<i>Активностью считаются действия сотрудника: отправка, чтение, "
            "перемещение писем. Входящие письма сами по себе активностью не "
            "считаются.</i>"
        )

        self.telegram.edit(chat_id, notice["message_id"], "\n".join(lines))

        if len(dormant) > 20:
            self._send_dormant_file(chat_id, org, dormant, days)

    @staticmethod
    def _matched(users: list[dict], scan) -> int:
        """Сколько сотрудников из справочника нашлись в журнале как активные."""
        return sum(
            1 for user in users if any(key in scan.active for key in user_keys(user))
        )

    def _send_dormant_debug(
        self, chat_id: int, message_id: int, users: list[dict], scan, days: int
    ) -> None:
        """Показывает, как журнал называет людей — чтобы поймать несовпадение."""
        lines = [
            f"<b>Отладка /dead за {days} дн.</b>",
            f"Событий: {scan.events} · активных ключей: {len(scan.active)}",
            f"Сотрудников: {len(users)} · опознано активными: "
            f"{self._matched(users, scan)}",
            "",
            "<b>Первые события журнала</b>",
        ]
        for sample in scan.samples:
            lines.append(
                f"• uid=<code>{html.escape(str(sample.get('userUid')))}</code> "
                f"login=<code>{html.escape(str(sample.get('userLogin')))}</code> "
                f"{html.escape(str(sample.get('eventType')))} "
                f"{str(sample.get('date'))[:19]}"
            )
        if not scan.samples:
            lines.append("журнал пуст — либо период мал, либо нет прав на аудит-лог")

        lines.append("")
        lines.append("<b>Как выглядят сотрудники</b>")
        for user in users[:3]:
            keys = sorted(user_keys(user))[:4]
            lines.append(
                f"• <code>{html.escape(str(user.get('email', '')))}</code> → "
                + ", ".join(f"<code>{html.escape(key)}</code>" for key in keys)
            )
        self.telegram.edit(chat_id, message_id, "\n".join(lines))

    def _send_dormant_file(self, chat_id: int, org: Org, dormant: list, days: int) -> None:
        fields = ["last", "first", "nickname", "email", "is_enabled", "created_at", "note"]
        rows = [{
            "last": (item.user.get("name") or {}).get("last", ""),
            "first": (item.user.get("name") or {}).get("first", ""),
            "nickname": item.user.get("nickname", ""),
            "email": item.email,
            "is_enabled": "да" if item.enabled else "нет",
            "created_at": item.created,
            "note": "письма приходят, но не читаются" if item.received_only else "",
        } for item in dormant]

        stamp = datetime.now().strftime("%Y-%m-%d")
        target = data_dir() / "exports" / f"{org.key}_dormant_{days}d_{stamp}"
        try:
            if xlsx.available():
                path = xlsx.write_users(target.with_suffix(".xlsx"), rows, fields)
            else:
                path = write_rows(target.with_suffix(".csv"), rows, fields)
            self.telegram.upload_document(
                chat_id, str(path), f"Без активности за {days} дн.: {len(rows)}"
            )
        except Exception:
            log.exception("Не собрал файл с неактивными")

    # --- копия данных и журнал ------------------------------------------------
    def cmd_backup(self, chat_id: int, quiet: bool = False) -> None:
        """Журнал, пароли, настройки и архив счетов — одним архивом в чат."""
        root = data_dir()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        archive = root / "backups" / f"ya360_{stamp}.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(root.rglob("*")):
                    if path.is_file() and "backups" not in path.parts:
                        bundle.write(path, path.relative_to(root))
        except OSError as exc:
            self.telegram.send(
                chat_id, f"Не собрал архив: <code>{html.escape(str(exc))}</code>"
            )
            return

        size = archive.stat().st_size
        if size <= 32:  # пустой zip — данных ещё нет
            archive.unlink(missing_ok=True)
            if not quiet:
                self.telegram.send(chat_id, "Сохранять пока нечего — данных нет.")
            return

        caption = (
            f"Копия данных бота от {stamp.replace('_', ' ')}"
            + (" (по расписанию)" if quiet else "")
        )
        if self.vault.enabled:
            caption += "\n⚠️ Внутри пароли сотрудников — храните бережно."
        self.telegram.upload_document(chat_id, str(archive), caption)
        self.state.remember("last_backup", time.time())
        archive.unlink(missing_ok=True)  # на сервере копию не держим

    def cmd_journal(self, chat_id: int) -> None:
        if not self.audit_path.exists():
            self.telegram.send(chat_id, "Журнал пока пуст.")
            return
        self.telegram.upload_document(
            chat_id, str(self.audit_path), "Журнал операций (открывается в Excel)"
        )

    def maybe_auto_backup(self) -> None:
        """Раз в BOT_BACKUP_DAYS суток шлёт копию в последний активный чат."""
        if not self.backup_days:
            return
        last = float(self.state.recall("last_backup", 0) or 0)
        if time.time() - last < self.backup_days * 86400:
            return
        chat_id = self.state.recall("last_chat")
        if not chat_id:
            return
        try:
            self.cmd_backup(int(chat_id), quiet=True)
        except (TelegramError, OSError):
            log.exception("Не отправил копию по расписанию")
            self.state.remember("last_backup", time.time())  # не долбим каждую секунду

    # --- последние заведённые -----------------------------------------------
    def cmd_last(self, chat_id: int, limit: int = 10) -> None:
        org = self.org_for(chat_id)
        if not self.audit_path.exists():
            self.telegram.send(chat_id, "Журнал пока пуст.")
            return
        try:
            rows = read_rows(self.audit_path)
        except OSError as exc:
            self.telegram.send(chat_id, f"Журнал не читается: {html.escape(str(exc))}")
            return

        created = [
            row for row in rows
            if row.get("action") == "create"
            and row.get("status") == "ok"
            and (len(self.registry) < 2 or row.get("org") == org.title)
        ]
        if not created:
            self.telegram.send(chat_id, "Через бота ещё никого не заводили.")
            return

        lines = [self.org_prefix(chat_id) + f"<b>Последние {min(limit, len(created))}</b>"]
        for row in reversed(created[-limit:]):
            when = str(row.get("at", ""))[:10]
            name = _join(row.get("last"), row.get("first"))
            lines.append(
                f"{when} — {html.escape(name)} "
                f"<code>{html.escape(str(row.get('email', '')))}</code>"
            )
        self.telegram.send(chat_id, "\n".join(lines))

    def ask_delete(self, chat_id: int, user_id: str) -> None:
        org = self.org_for(chat_id)
        if not org.allow_delete:
            self.telegram.send(
                chat_id,
                "Удаление выключено. Включите <code>allow_delete</code> для "
                "организации в orgs.json (или BOT_ALLOW_DELETE=true).",
            )
            return
        user = self.directory_for(chat_id).user_by_id(user_id)
        if not user:
            self.telegram.send(chat_id, "Сотрудник не найден.")
            return
        nickname = user.get("nickname") or str(user.get("email", "")).split("@")[0]
        self.pending_delete[chat_id] = {
            "user_id": user_id,
            "nickname": nickname,
            "email": user.get("email", ""),
            "name": _full_name(user),
            "org": org.key,
            "at": time.time(),
        }
        self.telegram.send(
            chat_id,
            f"⚠️ <b>Удаление без возврата</b>\n\n"
            f"{html.escape(_full_name(user))} — "
            f"<code>{html.escape(str(user.get('email', '')))}</code>\n"
            "Вся почта и файлы на Диске пропадут навсегда, восстановить нельзя.\n"
            "Обычно достаточно блокировки: данные сохранятся, вход закроется.\n\n"
            f"Если уверены, отправьте сообщением:\n<code>удалить {html.escape(nickname)}</code>",
        )

    def confirm_delete(self, chat_id: int, sender: dict, nickname: str) -> None:
        entry = self.pending_delete.get(chat_id)
        if not entry or time.time() - entry["at"] > 300:
            self.pending_delete.pop(chat_id, None)
            self.telegram.send(
                chat_id,
                "Нечего удалять: откройте карточку через /find и нажмите «Удалить».",
            )
            return
        if nickname.strip().lower() != entry["nickname"].lower():
            self.telegram.send(
                chat_id,
                f"Логин не совпал. Жду ровно: <code>удалить {html.escape(entry['nickname'])}</code>",
            )
            return

        self.pending_delete.pop(chat_id, None)
        org = self.registry.get(entry["org"])
        directory = self.registry.directory(org.key)
        try:
            directory.client.delete_user(entry["user_id"])
        except ApiError as exc:
            self._audit(
                org, "delete",
                NewUser(first="", last=entry["name"], nickname=entry["nickname"]),
                entry["email"], sender, "error", str(exc),
            )
            self.telegram.send(chat_id, "❌ Не удалён.\n" + _api_error(exc))
            return
        directory.invalidate()
        self._audit(
            org, "delete",
            NewUser(first="", last=entry["name"], nickname=entry["nickname"]),
            entry["email"], sender, "ok", "",
        )
        self.telegram.send(
            chat_id,
            f"🗑 Удалён: <code>{html.escape(str(entry['email']))}</code>",
        )

    # --- счета ------------------------------------------------------------------
    def cmd_bill(self, chat_id: int) -> None:
        org = self.org_for(chat_id)
        invoices = self.state.invoices(org.key, limit=12)
        counters = ""
        try:
            users = self.directory_for(chat_id).users()
            blocked = sum(1 for user in users if not user.get("isEnabled", True))
            counters = (
                f"Сотрудников: <b>{len(users)}</b>"
                + (f" · заблокированных: <b>{blocked}</b>" if blocked else "")
            )
            if blocked:
                counters += (
                    "\n<i>За заблокированных оплата обычно идёт — "
                    "проверьте /dead.</i>"
                )
        except ApiError:
            pass  # счётчик — приятное дополнение, без него команда всё равно работает

        text = [
            self.org_prefix(chat_id) + "<b>Оплата</b>",
            f'Счёт и закрывающие документы: <a href="{org.billing_url}">админка → '
            "Финансы → Оплата и тариф</a>.",
            "<i>API 360 счета не отдаёт, поэтому выгрузить их автоматически нельзя — "
            "скачайте из админки и пришлите сюда файлом, я сохраню в архив.</i>",
        ]
        if counters:
            text.insert(1, counters)
        markup = None
        if invoices:
            text.append("\n<b>Архив</b>")
            markup = {"inline_keyboard": [
                [{
                    "text": f"{item['date'][:10]} · {item['name']}"[:64],
                    "callback_data": f"inv:{item['id']}",
                }]
                for item in invoices
            ]}
        self.telegram.send(chat_id, "\n".join(text), reply_markup=markup)

    def save_invoice(self, chat_id: int, sender: dict, message: dict) -> None:
        org = self.org_for(chat_id)
        document = message["document"]
        invoice_id = str(document["file_unique_id"])
        name = document.get("file_name") or f"{invoice_id}.pdf"
        stored = ""
        try:
            destination = self.invoices_dir / org.key / f"{datetime.now():%Y-%m}" / name
            self.telegram.download_file(document["file_id"], destination)
            stored = str(destination)
        except Exception as exc:  # архив на диске — приятный бонус, не критичный
            log.warning("Не удалось сохранить файл на диск: %s", exc)

        self.state.add_invoice(org.key, {
            "id": invoice_id,
            "file_id": document["file_id"],
            "name": name,
            "size": document.get("file_size", 0),
            "date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "path": stored,
            "by": f"{sender.get('id')} @{sender.get('username', '')}".strip(),
            "caption": (message.get("caption") or "")[:200],
        })
        self.telegram.send(
            chat_id,
            f"📎 Сохранил в архив «{html.escape(org.title)}»: "
            f"<b>{html.escape(name)}</b>\nСписок — /bill",
        )

    def send_invoice(self, chat_id: int, invoice_id: str) -> None:
        org = self.org_for(chat_id)
        item = self.state.invoice(org.key, invoice_id)
        if not item:
            self.telegram.send(chat_id, "Документ не найден в архиве.")
            return
        caption = f"{item['name']} · {item['date'][:10]}"
        try:
            self.telegram.send_document(chat_id, item["file_id"], caption)
            return
        except TelegramError as exc:
            log.warning("file_id не сработал (%s), пробую с диска", exc)
        path = item.get("path")
        if path and Path(path).exists():
            self.telegram.upload_document(chat_id, path, caption)
        else:
            self.telegram.send(chat_id, "Файл больше недоступен.")

    # --- кнопки ------------------------------------------------------------------
    def on_callback(self, callback: dict) -> None:
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        data = callback.get("data") or ""

        if sender.get("id") not in self.allowed:
            self.telegram.answer_callback(callback["id"], "Нет доступа")
            return

        action, _, payload = data.partition(":")
        self.telegram.answer_callback(callback["id"])

        if action == "org":
            self.state.set_chat_org(chat_id, payload)
            self.telegram.edit(
                chat_id, message_id,
                f"Организация: <b>{html.escape(self.registry.get(payload).title)}</b>",
            )
        elif action == "mk":
            self.do_create(chat_id, message_id, sender, payload)
        elif action == "mkno":
            self.pending.pop(payload, None)
            self.telegram.edit(chat_id, message_id, "✖️ Отменено")
        elif action == "u":
            self.show_card(chat_id, payload, message_id)
        elif action == "blk":
            user_id, _, flag = payload.partition(":")
            self.do_block(chat_id, message_id, sender, user_id, flag == "1")
        elif action == "pwd":
            self.do_password(chat_id, message_id, sender, payload)
        elif action == "pv":
            self.show_saved_password(chat_id, payload)
        elif action == "wel":
            self.send_welcome(chat_id, payload)
        elif action == "cfg":
            if payload == "back":
                self.cmd_settings(chat_id, message_id)
            else:
                self.show_setting_choices(chat_id, message_id, payload)
        elif action == "cfgs":
            field, _, index = payload.partition(":")
            self.apply_setting(chat_id, message_id, field, index)
        elif action == "del":
            self.ask_delete(chat_id, payload)
        elif action == "inv":
            self.send_invoice(chat_id, payload)

    # --- разбор ввода ----------------------------------------------------------
    @staticmethod
    def parse_input(text: str, order: str = "last_first") -> NewUser:
        """«Фамилия Имя [Отчество] | отдел | должность | телефон».

        При BOT_NAME_ORDER=first_last ждём обратный порядок: «Имя Фамилия».
        """
        parts = [part.strip() for part in text.split("|")]
        fio = parts[0].split()
        expected = (
            "Имя Фамилия Отчество" if order == "first_last" else "Фамилия Имя Отчество"
        )
        if len(fio) < 2:
            raise ValueError(f"Нужны минимум два слова — жду «{expected}».")
        if len(fio) > 3:
            raise ValueError(f"Слишком много слов в ФИО — жду «{expected}».")

        if order == "first_last":
            user = NewUser(first=fio[0], last=fio[1])
        else:
            user = NewUser(last=fio[0], first=fio[1])
        user.middle = fio[2] if len(fio) > 2 else ""

        # «@логин» в любом месте задаёт адрес вручную, остальное — по порядку
        rest = []
        for part in parts[1:]:
            if part.startswith("@"):
                user.nickname = part[1:].strip().split("@")[0]
            else:
                rest.append(part)
        if len(rest) > 0 and rest[0]:
            user.department_name = rest[0]
        if len(rest) > 1 and rest[1]:
            user.position = rest[1]
        if len(rest) > 2 and rest[2]:
            user.phone = rest[2]
        return user

    # --- служебное ---------------------------------------------------------------
    def _report_error(self, update: dict, exc: Exception) -> None:
        """Молчащий бот хуже бота с ошибкой — пишем в чат, а не только в лог."""
        message = update.get("message") or (update.get("callback_query") or {}).get(
            "message"
        ) or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not chat_id:
            return
        try:
            self.telegram.send(
                chat_id,
                "⚠️ Внутренняя ошибка: "
                f"<code>{html.escape(self.telegram.safe(exc))}</code>",
            )
        except TelegramError:
            pass

    def _gc_pending(self, ttl: int = 900) -> None:
        now = time.time()
        for key in [k for k, v in self.pending.items() if now - v["created_at"] > ttl]:
            self.pending.pop(key, None)

    def _process_deletions(self) -> None:
        now = time.time()
        remaining = []
        for chat_id, message_id, when in self.to_delete:
            if when <= now:
                self.telegram.delete(chat_id, message_id)
            else:
                remaining.append((chat_id, message_id, when))
        self.to_delete = remaining

    def _audit_user(
        self, org: Org, action: str, user: dict, sender: dict,
        status: str, error: str, password: str = "",
    ) -> None:
        name = user.get("name") or {}
        self._audit(
            org, action,
            NewUser(
                first=name.get("first", ""),
                last=name.get("last", ""),
                middle=name.get("middle", ""),
                nickname=user.get("nickname", ""),
                position=user.get("position", ""),
                password=password,
            ),
            str(user.get("email", "")), sender, status, error,
        )

    def _audit(
        self, org: Org, action: str, user: NewUser, email: str,
        sender: dict, status: str, error: str,
    ) -> None:
        row = {
            "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "org": org.title,
            "action": action,
            "last": user.last,
            "first": user.first,
            "middle": user.middle,
            "nickname": user.nickname,
            "email": email,
            "password": user.password if self.log_passwords else "",
            "department": user.department_name or user.department_id,
            "position": user.position,
            "telegram_user": f"{sender.get('id')} @{sender.get('username', '')}".strip(),
            "status": status,
            "error": error,
        }
        try:
            existing = read_rows(self.audit_path) if self.audit_path.exists() else []
            write_rows(self.audit_path, existing + [row], AUDIT_FIELDS)
        except Exception:
            log.exception("Не удалось записать журнал %s", self.audit_path)


def _full_name(user: dict) -> str:
    name = user.get("name") or {}
    return _join(name.get("last"), name.get("first"), name.get("middle")) or str(
        user.get("nickname", "")
    )


def _join(*parts) -> str:
    return " ".join(str(part) for part in parts if part)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _who(sender: dict) -> str:
    return f"{sender.get('id')} @{sender.get('username', '')}".strip()


def _api_error(exc: ApiError) -> str:
    if exc.status == 401:
        return (
            "🔑 <b>Токен Яндекса больше не принимается.</b>\n"
            "Он живёт около года — похоже, истёк или отозван.\n\n"
            "Выпустите новый под администратором организации:\n"
            "oauth.yandex.ru/authorize?response_type=token&amp;client_id=ВАШ_CLIENT_ID\n"
            "и впишите в <code>.env</code> в строку <code>Y360_TOKEN</code>, "
            "затем <code>docker compose up -d</code>."
        )
    if exc.status == 403:
        return (
            "🚫 Яндекс отказал в доступе. Проверьте, что токен выдан "
            "администратору организации и приложению выданы права "
            "<code>directory:write_users</code>."
        )
    return f"Ошибка API: <code>{html.escape(str(exc))}</code>"


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "да", "on"}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()
    try:
        Bot().run()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except TelegramError as exc:
        raise SystemExit(f"Telegram: {exc}")


if __name__ == "__main__":
    main()
