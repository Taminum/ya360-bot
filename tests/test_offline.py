"""Оффлайн-проверки без обращения к API: python tests/test_offline.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from y360.csvio import read_rows  # noqa: E402
from y360.names import generate_password, make_nickname, translit, unique_nickname  # noqa: E402
from y360.service import Directory, NewUser  # noqa: E402


class FakeClient:
    """Подменяет Y360Client: ничего не шлёт в сеть, помнит созданных."""

    def __init__(self):
        self.created: list[dict] = []
        self.aliases: list[tuple[str, str]] = []

    def departments(self):
        return [
            {"id": 1, "name": "Все сотрудники", "membersCount": 2},
            {"id": 5, "name": "Отдел продаж", "membersCount": 1},
            {"id": 7, "name": "Бухгалтерия", "membersCount": 1},
        ]

    def users(self):
        return [
            {
                "id": "1130000000000001",
                "nickname": "ivan.ivanov",
                "email": "ivan.ivanov@example.ru",
                "aliases": ["sales"],
                "name": {"first": "Иван", "last": "Иванов", "middle": ""},
                "isEnabled": True,
            }
        ]

    def create_user(self, payload):
        self.created.append(payload)
        return {
            "id": f"113000000000{len(self.created):04d}",
            "email": f"{payload['nickname']}@example.ru",
            **payload,
        }

    def add_alias(self, user_id, alias):
        self.aliases.append((user_id, alias))
        return {"alias": alias}




class _Recorder:
    """Подменяет клиент Telegram: запоминает сообщения вместо отправки."""

    def __init__(self, sink):
        self.sink = sink

    def send(self, chat_id, text, **kwargs):
        self.sink.append({"text": text, "markup": kwargs.get("reply_markup")})
        return {"message_id": len(self.sink)}

    def edit(self, chat_id, message_id, text, **kwargs):
        self.sink.append({"text": text, "markup": kwargs.get("reply_markup")})
        return {"message_id": message_id}


class _FakeDirectory:
    """Только для проверки поиска двойников."""

    @staticmethod
    def users():
        return [
            {
                "id": "1", "email": "ivan.ivanov@example.ru", "isEnabled": True,
                "name": {"first": "Иван", "last": "Иванов"},
            },
            {
                "id": "2", "email": "maria.petrova@example.ru", "isEnabled": True,
                "name": {"first": "Мария", "last": "Петрова"},
            },
        ]


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok  {message}")


def test_translit():
    print("транслитерация")
    check(translit("Иванов") == "ivanov", "Иванов -> ivanov")
    check(translit("Щербакова") == "shcherbakova", "Щербакова -> shcherbakova")
    check(translit("Юлия") == "iuliia", "Юлия -> iuliia")
    check(make_nickname("Пётр", "Сидоров") == "petr.sidorov", "шаблон {first}.{last}")
    check(
        make_nickname("Мария", "Петрова", "{f}{last}") == "mpetrova",
        "шаблон {f}{last}",
    )
    check(
        unique_nickname("ivan.ivanov", {"ivan.ivanov", "ivan.ivanov2"}) == "ivan.ivanov3",
        "занятый логин получает суффикс",
    )


def test_password():
    print("пароли")
    for _ in range(200):
        password = generate_password(14)
        check_silent = len(password) == 14
        if not check_silent:
            raise AssertionError("длина пароля")
        if not (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(not c.isalnum() for c in password)
        ):
            raise AssertionError(f"слабый пароль: {password}")
    check(True, "200 паролей: длина и состав символов в порядке")
    check(len(generate_password(50)) == 20, "длина ограничена 20 символами (лимит 360)")


def test_csv():
    print("чтение CSV")
    rows = read_rows(Path(__file__).parent.parent / "examples" / "users.csv")
    check(len(rows) == 3, "прочитано 3 строки")
    check(rows[0]["last"] == "Иванов", "русская шапка распознана (Фамилия -> last)")
    check(rows[1]["aliases"] == "buh", "алиасы прочитаны")
    check(rows[2]["nickname"] == "p.sidorov", "явный логин прочитан")


def test_directory_flow():
    print("подготовка и создание сотрудника")
    directory = Directory(FakeClient())

    dept_id, dept_name = directory.resolve_department("продаж")
    check((dept_id, dept_name) == (5, "Отдел продаж"), "отдел ищется по части имени")
    check(directory.resolve_department("")[0] == 1, "пустой отдел -> корневой")
    check(directory.resolve_department("7")[1] == "Бухгалтерия", "отдел по ID")

    try:
        directory.resolve_department("Склад")
    except ValueError:
        check(True, "несуществующий отдел -> понятная ошибка")
    else:
        raise AssertionError("ожидалась ошибка по отделу «Склад»")

    # Иванов Иван уже есть -> логин должен стать ivan.ivanov2
    user = NewUser(first="Иван", last="Иванов", department_id=5)
    directory.prepare(user)
    check(user.nickname == "ivan.ivanov2", "коллизия логина разрешена: ivan.ivanov2")
    check(bool(user.password), "пароль сгенерирован")
    check(user.display_name == "Иванов Иван", "отображаемое имя заполнено")

    user.position = "Менеджер"
    user.phone = "+79990000001"
    user.aliases = ["sales.ivanov"]
    created = directory.create(user)
    payload = directory.client.created[0]
    check(payload["nickname"] == "ivan.ivanov2", "в API ушёл верный логин")
    check(payload["departmentId"] == 5, "в API ушёл ID отдела")
    check(payload["name"]["first"] == "Иван", "имя передано")
    check(payload["passwordChangeRequired"] is True, "смена пароля при входе включена")
    check(
        payload["contacts"][0] == {"type": "phone", "value": "+79990000001"},
        "телефон ушёл в contacts",
    )
    check(created["email"] == "ivan.ivanov2@example.ru", "адрес возвращён")
    check(directory.client.aliases[0][1] == "sales.ivanov", "алиас добавлен")

    # следующий однофамилец не должен получить тот же логин
    second = NewUser(first="Иван", last="Иванов")
    directory.prepare(second)
    check(second.nickname == "ivan.ivanov3", "кэш логинов обновляется на лету")


def test_bot_parsing():
    print("разбор сообщений бота")
    from bot import Bot

    user = Bot.parse_input("Иванов Иван")
    check((user.last, user.first) == ("Иванов", "Иван"), "«Фамилия Имя»")

    user = Bot.parse_input("Иванов Иван Иванович | Отдел продаж | Менеджер | +79990000001")
    check(user.middle == "Иванович", "отчество")
    check(user.department_name == "Отдел продаж", "отдел из сообщения")
    check(user.position == "Менеджер", "должность")
    check(user.phone == "+79990000001", "телефон")

    for bad in ["Иванов", "Иванов Иван Иванович Петрович"]:
        try:
            Bot.parse_input(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"ожидалась ошибка для: {bad}")
    check(True, "некорректный ввод отклоняется")

    swapped = Bot.parse_input("Сакыбек Барысхан", "first_last")
    check(
        (swapped.first, swapped.last) == ("Сакыбек", "Барысхан"),
        "порядок first_last: «Имя Фамилия»",
    )
    from y360.names import make_nickname
    check(
        make_nickname(swapped.first, swapped.last, "{f}.{last}") == "s.baryskhan",
        "паспортная транслитерация: s.baryskhan",
    )
    check(
        make_nickname(swapped.first, swapped.last, "{f}.{last}", "simple") == "s.baryshan",
        "простая транслитерация: s.baryshan",
    )

    manual = Bot.parse_input("Иванов Иван | @i.ivanov | Продажи | Менеджер")
    check(manual.nickname == "i.ivanov", "логин можно задать через @")
    check(manual.department_name == "Продажи", "отдел не сбился из-за @логина")
    check(manual.position == "Менеджер", "должность не сбилась")


def test_orgs_registry():
    print("реестр организаций")
    import json
    import os
    import tempfile

    from y360.orgs import OrgRegistry

    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "orgs.json"
        config.write_text(
            json.dumps({"orgs": [
                {"name": "ООО Ромашка", "org_id": "111", "token": "t1",
                 "domain": "romashka.ru", "allow_delete": True},
                {"key": "vtoraia", "name": "ИП Иванов", "org_id": "222",
                 "token": "$SECOND_TOKEN"},
            ]}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.environ["SECOND_TOKEN"] = "t2"
        registry = OrgRegistry.load(config)

        check(len(registry) == 2, "прочитаны две организации")
        check(registry.default.key == "ooo.romashka", "ключ по умолчанию из названия")
        check(registry.get("vtoraia").token == "t2", "токен подставлен из окружения")
        check(registry.get("vtoraia").org_id == "222", "ID второй организации")
        check(registry.default.allow_delete is True, "флаг удаления читается")
        check(registry.get("нет-такой").key == registry.default.key,
              "неизвестный ключ -> организация по умолчанию")
        check(registry.has("vtoraia") and not registry.has("нет"), "has() работает")

        first = registry.directory(None)
        check(first is registry.directory("ooo.romashka"), "Directory кэшируется")
        check(first.client.org_id == "111", "клиент нацелен на нужную организацию")
        check(
            registry.directory("vtoraia").client.org_id == "222",
            "у второй организации свой клиент",
        )


def test_state():
    print("состояние бота")
    import tempfile

    from y360.state import State

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        state = State(path)
        state.set_chat_org(555, "vtoraia")
        state.add_invoice("vtoraia", {
            "id": "abc", "file_id": "f1", "name": "schet.pdf",
            "date": "2026-09-01T10:00:00+03:00", "path": "",
        })

        restored = State(path)
        check(restored.chat_org(555) == "vtoraia", "выбранная организация пережила рестарт")
        check(restored.chat_org(999) is None, "для нового чата организация не задана")
        check(len(restored.invoices("vtoraia")) == 1, "счёт сохранён")
        check(restored.invoice("vtoraia", "abc")["name"] == "schet.pdf", "счёт находится по id")
        check(restored.invoice("vtoraia", "нет") is None, "чужой id -> None")


def test_delete_guard():
    print("подтверждение удаления")
    from bot import DELETE_PHRASE

    check(DELETE_PHRASE.match("удалить ivan.ivanov") is not None, "фраза распознана")
    check(DELETE_PHRASE.match("Удалить ivan.ivanov") is not None, "регистр не важен")
    check(DELETE_PHRASE.match("удалить") is None, "без логина не считается")
    check(
        DELETE_PHRASE.match("удалить ivan.ivanov и всех") is None,
        "лишние слова не проходят",
    )
    check(
        DELETE_PHRASE.match("удалить ivan.ivanov").group(1) == "ivan.ivanov",
        "логин извлекается",
    )


def test_password_store():
    print("хранилище паролей")
    import tempfile

    from y360.state import PasswordStore

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "passwords.json"

        off = PasswordStore(path, enabled=False)
        off.save("romashka", "ivan.ivanov", "secret")
        check(not path.exists(), "выключенное хранилище ничего не пишет")

        vault = PasswordStore(path, enabled=True)
        vault.save("romashka", "ivan.ivanov", "secret1", email="i@r.ru", at="2026-09-01")
        vault.save("vtoraya", "ivan.ivanov", "secret2")
        check(vault.get("romashka", "ivan.ivanov")["password"] == "secret1",
              "пароль сохраняется и читается")
        check(vault.get("vtoraya", "ivan.ivanov")["password"] == "secret2",
              "организации не путаются между собой")
        check(vault.get("romashka", "IVAN.IVANOV")["password"] == "secret1",
              "регистр логина не важен")
        check(vault.get("romashka", "нет.такого") is None, "чужой логин -> None")

        vault.save("romashka", "ivan.ivanov", "secret3")
        restored = PasswordStore(path, enabled=True)
        check(restored.get("romashka", "ivan.ivanov")["password"] == "secret3",
              "новый пароль вытесняет старый и переживает рестарт")
        check(restored.all_for("romashka") == {"ivan.ivanov": "secret3"},
              "выборка по организации для выгрузки")


def test_xlsx():
    print("выгрузка в Excel")
    import tempfile

    from y360 import xlsx

    if not xlsx.available():
        check(True, "openpyxl не установлен — пропускаю")
        return

    from openpyxl import load_workbook

    with tempfile.TemporaryDirectory() as tmp:
        rows = [
            {"last": "Иванов", "first": "Иван", "nickname": "ivan.ivanov",
             "email": "i@r.ru", "password": "sec", "is_enabled": "да"},
            {"last": "Петрова", "first": "Мария", "nickname": "maria.petrova",
             "email": "m@r.ru", "password": "", "is_enabled": "нет"},
        ]
        fields = ["last", "first", "nickname", "email", "password", "is_enabled"]
        path = xlsx.write_users(Path(tmp) / "users.xlsx", rows, fields)

        sheet = load_workbook(path).active
        check(sheet.title == "Сотрудники", "лист назван по-человечески")
        check([c.value for c in sheet[1]][:2] == ["Фамилия", "Имя"],
              "шапка переведена на русский")
        check(sheet.max_row == 3, "две строки данных под шапкой")
        check(sheet.freeze_panes == "A2" and sheet.auto_filter.ref,
              "шапка закреплена, фильтр включён")
        check([c.value for c in sheet[2]][4] == "sec", "пароль попал в таблицу")


def test_bot_wiring():
    print("сборка бота (без сети)")
    import os
    import tempfile

    import bot as bot_module

    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update({
            "TELEGRAM_BOT_TOKEN": "1:TEST",
            "TELEGRAM_ALLOWED_USERS": "111, 222",
            "Y360_TOKEN": "t",
            "Y360_ORG_ID": "999",
            "Y360_ORG_NAME": "Тестовая компания",
            "DATA_DIR": tmp,
            "Y360_ORGS_FILE": str(Path(tmp) / "нет.json"),
            "BOT_ALLOW_DELETE": "true",
            "BOT_STORE_PASSWORDS": "true",
        })
        instance = bot_module.Bot()

        check(instance.allowed == {111, 222}, "белый список разобран")
        check(instance.org_for(1).org_id == "999", "организация из окружения")
        check(instance.org_for(1).allow_delete is True, "BOT_ALLOW_DELETE учтён")
        check(instance.org_prefix(1) == "", "для одной организации нет шапки")
        check(
            instance.directory_for(1).client.org_id == "999",
            "Directory собирается под организацию",
        )
        check(str(instance.audit_path).startswith(tmp), "журнал пишется в DATA_DIR")

        user = {
            "id": "1", "email": "i.i@example.ru", "isEnabled": True,
            "name": {"first": "Иван", "last": "Иванов"}, "aliases": ["sales"],
            "departmentId": 5, "position": "Менеджер",
        }
        card = instance.card_text(1, user)
        check("Иванов Иван" in card and "i.i@example.ru" in card, "карточка собирается")
        check("🟢 активен" in card, "статус в карточке")
        buttons = instance.card_buttons(1, user)["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        check("🔒 Заблокировать" in labels, "кнопка блокировки")
        check("🗑 Удалить" in labels, "кнопка удаления при allow_delete")
        check("🔐 Выданный пароль" in labels, "кнопка сохранённого пароля")
        check(instance.vault.enabled, "хранилище паролей включено флагом")

        os.environ["BOT_ALLOW_DELETE"] = "false"
        instance2 = bot_module.Bot()
        labels2 = [
            b["text"]
            for row in instance2.card_buttons(1, user)["inline_keyboard"]
            for b in row
        ]
        check("🗑 Удалить" not in labels2, "без allow_delete кнопки удаления нет")

        # настройки, заданные из бота, перекрывают .env
        from y360.state import State as _State
        org = instance.org_for(1)
        check(instance.setting(org, "password_length") == 14, "длина пароля из .env")
        instance.state.set_setting(org.key, "password_length", 20)
        instance.state.set_setting(org.key, "nickname_pattern", "{f}.{last}")
        check(instance.setting(org, "password_length") == 20, "настройка перекрыла .env")
        check(instance.setting(org, "nickname_pattern") == "{f}.{last}", "шаблон логина")
        check(
            _State(instance.state.path).settings(org.key)["password_length"] == 20,
            "настройки переживают перезапуск",
        )

        import bot as _bot
        for field, (title, choices) in _bot.SETTINGS_CHOICES.items():
            check(bool(title) and len(choices) >= 2, f"настройка «{title}» с выбором")

        check(bool(_bot.LOOKS_LIKE_LOGIN.match("m.ivanov")), "логин распознан как поиск")
        check(
            bool(_bot.LOOKS_LIKE_LOGIN.match("m.ivanov@blagotrud.ru")),
            "почта распознана как поиск",
        )
        check(
            not _bot.LOOKS_LIKE_LOGIN.match("Иванов Максим"),
            "ФИО не путается с логином",
        )

        # текст для сотрудника и меню настроек — что реально уходит в чат
        sent: list[dict] = []
        instance.telegram = _Recorder(sent)
        instance.welcome["w1"] = {
            "email": "m.ivanov@blagotrud.ru", "password": "+G528AQ-L*Rit#",
            "name": "Максим", "change": True, "at": 0,
        }
        instance.send_welcome(1, "w1")
        welcome = sent[-1]["text"]
        check("Здравствуйте, Максим!" in welcome, "обращение по имени")
        check("m.ivanov@blagotrud.ru" in welcome, "логин в тексте для сотрудника")
        check("+G528AQ-L*Rit#" in welcome, "пароль в тексте")
        check("mail.yandex.ru" in welcome, "ссылка на вход")
        check("сменить пароль" in welcome, "напоминание про смену пароля")

        instance.send_welcome(1, "нет-такого")
        check("устарел" in sent[-1]["text"], "просроченный текст не падает")

        instance.cmd_settings(1)
        menu = sent[-1]
        check("Настройки" in menu["text"], "меню настроек открывается")
        check("20 символов" in menu["text"], "показана текущая длина пароля")
        check(
            len(menu["markup"]["inline_keyboard"]) == len(_bot.SETTINGS_CHOICES),
            "кнопка на каждую настройку",
        )

        twins = _bot.Bot.find_twins(_FakeDirectory(), NewUser(first="Иван", last="Иванов"))
        check(len(twins) == 1, "двойник найден по имени и фамилии")
        check(
            not _bot.Bot.find_twins(_FakeDirectory(), NewUser(first="Пётр", last="Иванов")),
            "другое имя двойником не считается",
        )


def main():
    for test in [
        test_translit,
        test_password,
        test_csv,
        test_directory_flow,
        test_bot_parsing,
        test_orgs_registry,
        test_state,
        test_delete_guard,
        test_password_store,
        test_xlsx,
        test_bot_wiring,
    ]:
        test()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
