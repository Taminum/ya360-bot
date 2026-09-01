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
from datetime import datetime, timezone
from pathlib import Path

from y360.api import ApiError
from y360.config import load_env, require
from y360.csvio import read_rows, write_rows
from y360.names import generate_password
from y360.orgs import Org, OrgRegistry
from y360.service import Directory, NewUser
from y360.state import State, data_dir
from y360.telegram import TelegramClient, TelegramError

log = logging.getLogger("y360.bot")

HELP = """<b>Что умею</b>

<b>Завести сотрудника</b> — просто отправьте ФИО:
<code>Иванов Иван</code>
<code>Иванов Иван Иванович</code>
<code>Иванов Иван | Отдел продаж | Менеджер | +79990000001</code>

<b>Команды</b>
/find Иванов — карточка сотрудника: блокировка, сброс пароля, удаление
/dept — отделы с их ID
/org — выбрать организацию
/bill — оплата и архив счетов
/id — ваш Telegram ID
/help — эта справка

Счёт на оплату можно просто прислать сюда файлом — сохраню в архив организации."""

AUDIT_FIELDS = [
    "at", "org", "action", "last", "first", "middle", "nickname", "email",
    "password", "department", "position", "telegram_user", "status", "error",
]

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
        self.log_passwords = _flag("BOT_LOG_PASSWORDS", False)
        self.audit_path = Path(
            os.environ.get("BOT_AUDIT_FILE", str(data_dir() / "journal.csv"))
        )
        self.invoices_dir = Path(
            os.environ.get("BOT_INVOICES_DIR", str(data_dir() / "invoices"))
        )
        self.pending: dict[str, dict] = {}
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

    def org_prefix(self, chat_id: int) -> str:
        if len(self.registry) < 2:
            return ""
        return f"<i>{html.escape(self.org_for(chat_id).title)}</i>\n"

    # --- цикл -------------------------------------------------------------
    def run(self) -> None:
        me = self.telegram.call("getMe")
        log.info(
            "Бот @%s запущен. Организаций: %s, доступ у %s пользователей",
            me.get("username"), len(self.registry), len(self.allowed),
        )
        self.telegram.set_commands([
            ("find", "Найти сотрудника"),
            ("dept", "Список отделов"),
            ("org", "Выбрать организацию"),
            ("bill", "Оплата и счета"),
            ("help", "Справка"),
            ("id", "Ваш Telegram ID"),
        ])
        for update in self.telegram.poll():
            try:
                self.handle(update)
            except Exception:  # одно кривое сообщение не должно ронять бота
                log.exception("Ошибка обработки обновления")
            self._process_deletions()

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

        if message.get("document"):
            self.save_invoice(chat_id, sender, message)
            return
        if not text:
            return

        if text.startswith("/help"):
            self.telegram.send(chat_id, self.org_prefix(chat_id) + HELP)
        elif text.startswith("/org"):
            self.cmd_orgs(chat_id)
        elif text.startswith("/dept"):
            self.cmd_departments(chat_id)
        elif text.startswith("/find"):
            self.cmd_find(chat_id, text[len("/find"):].strip())
        elif text.startswith("/bill"):
            self.cmd_bill(chat_id)
        elif text.startswith("/"):
            self.telegram.send(chat_id, "Неизвестная команда. /help")
        elif DELETE_PHRASE.match(text):
            self.confirm_delete(chat_id, sender, DELETE_PHRASE.match(text).group(1))
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
            self.telegram.send(chat_id, "Например: <code>/find Иванов</code>")
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
            new_user = self.parse_input(text)
        except ValueError as exc:
            self.telegram.send(chat_id, f"{html.escape(str(exc))}\n\n{HELP}")
            return

        try:
            department_id, department_name = directory.resolve_department(
                new_user.department_name or org.default_department
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
        new_user.password_change_required = org.password_change_required

        try:
            directory.prepare(
                new_user,
                nickname_pattern=org.nickname_pattern,
                password_length=org.password_length,
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
            f"Отдел: {html.escape(new_user.department_name or str(department_id))}",
        ]
        if new_user.position:
            preview.append(f"Должность: {html.escape(new_user.position)}")
        if new_user.phone:
            preview.append(f"Телефон: {html.escape(new_user.phone)}")
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
            f"Почта: <code>{html.escape(email)}</code>",
            f"Пароль: <code>{html.escape(new_user.password)}</code>",
        ]
        if new_user.password_change_required:
            lines.append("\n<i>Пароль нужно сменить при первом входе.</i>")
        for error in created.get("_alias_errors", []):
            lines.append(f"⚠️ алиас не добавлен — {html.escape(error)}")
        if self.auto_delete:
            lines.append(f"<i>Сообщение удалится через {self.auto_delete} с.</i>")

        self.telegram.edit(chat_id, message_id, "\n".join(lines))
        self._audit(org, "create", new_user, email, sender, "ok", "")
        if self.auto_delete:
            self.to_delete.append((chat_id, message_id, time.time() + self.auto_delete))

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
        password = generate_password(org.password_length)
        try:
            directory.client.set_password(
                user_id, password, org.password_change_required
            )
        except ApiError as exc:
            self._audit_user(org, "password", user, sender, "error", str(exc))
            self.telegram.send(chat_id, _api_error(exc))
            return

        self._audit_user(
            org, "password", user, sender, "ok", "",
            password=password if self.log_passwords else "",
        )
        lines = [
            f"🔑 <b>{html.escape(_full_name(user))}</b>",
            f"Почта: <code>{html.escape(str(user.get('email', '')))}</code>",
            f"Новый пароль: <code>{html.escape(password)}</code>",
        ]
        if org.password_change_required:
            lines.append("\n<i>Сменить при первом входе.</i>")
        sent = self.telegram.send(chat_id, "\n".join(lines))
        if self.auto_delete:
            self.to_delete.append(
                (chat_id, sent["message_id"], time.time() + self.auto_delete)
            )
        _ = message_id

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
        text = [
            self.org_prefix(chat_id) + "<b>Оплата</b>",
            f'Счёт и закрывающие документы: <a href="{org.billing_url}">админка → '
            "Финансы → Оплата и тариф</a>.",
            "<i>API 360 счета не отдаёт, поэтому выгрузить их автоматически нельзя — "
            "скачайте из админки и пришлите сюда файлом, я сохраню в архив.</i>",
        ]
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
        elif action == "del":
            self.ask_delete(chat_id, payload)
        elif action == "inv":
            self.send_invoice(chat_id, payload)

    # --- разбор ввода ----------------------------------------------------------
    @staticmethod
    def parse_input(text: str) -> NewUser:
        """«Фамилия Имя [Отчество] | отдел | должность | телефон»."""
        parts = [part.strip() for part in text.split("|")]
        fio = parts[0].split()
        if len(fio) < 2:
            raise ValueError("Нужны минимум фамилия и имя.")
        if len(fio) > 3:
            raise ValueError("Слишком много слов в ФИО — жду «Фамилия Имя Отчество».")

        user = NewUser(last=fio[0], first=fio[1], middle=fio[2] if len(fio) > 2 else "")
        if len(parts) > 1 and parts[1]:
            user.department_name = parts[1]
        if len(parts) > 2 and parts[2]:
            user.position = parts[2]
        if len(parts) > 3 and parts[3]:
            user.phone = parts[3]
        return user

    # --- служебное ---------------------------------------------------------------
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


def _api_error(exc: ApiError) -> str:
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
