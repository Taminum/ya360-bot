"""CLI для массовых операций: python -m y360 <команда>."""

import argparse
import logging
import sys
import time
from datetime import datetime

from .api import ApiError, Y360Client
from .config import load_env
from .csvio import GENDER_ALIASES, read_rows, write_rows
from .orgs import OrgRegistry
from .service import Directory, NewUser

log = logging.getLogger("y360.cli")

CREATE_RESULT_FIELDS = [
    "status", "last", "first", "middle", "nickname", "email", "password",
    "department", "position", "error",
]

EXPORT_FIELDS = [
    "id", "last", "first", "middle", "nickname", "email", "aliases",
    "department_id", "position", "phone", "is_enabled", "is_admin",
    "is_dismissed", "created_at",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m y360",
        description="Массовые операции с сотрудниками Яндекс 360",
    )
    parser.add_argument(
        "-O", "--org-key",
        help="ключ организации из orgs.json (по умолчанию первая)",
    )
    parser.add_argument("--token", help="OAuth-токен вместо настроенного")
    parser.add_argument("--org", help="ID организации вместо настроенного")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check", help="проверить токен и доступ к организации по шагам"
    )
    sub.add_parser("orgs", help="настроенные организации и их ID")
    sub.add_parser("dept", help="список отделов")

    export = sub.add_parser("export", help="выгрузить всех сотрудников в CSV")
    export.add_argument("-o", "--out", default="out/users.csv")

    create = sub.add_parser("create", help="создать сотрудников из CSV")
    create.add_argument("-f", "--file", required=True, help="входной CSV")
    create.add_argument("-o", "--out", help="файл с результатом (логины и пароли)")
    create.add_argument("--dry-run", action="store_true", help="только проверка, без создания")
    create.add_argument("--delay", type=float, default=0.3, help="пауза между запросами, с")
    create.add_argument(
        "--department", default="", help="отдел по умолчанию (имя или ID)"
    )
    create.add_argument(
        "--nickname-pattern", default="{first}.{last}",
        help="шаблон логина: {first} {last} {f} {l}",
    )
    create.add_argument("--password-length", type=int, default=14)
    create.add_argument(
        "--no-password-change", action="store_true",
        help="не требовать смену пароля при первом входе",
    )

    disable = sub.add_parser("disable", help="заблокировать сотрудников (увольнение)")
    disable.add_argument(
        "targets", nargs="*", help="логины или email; либо используйте -f CSV"
    )
    disable.add_argument("-f", "--file", help="CSV со столбцом логина/почты")
    disable.add_argument("--enable", action="store_true", help="наоборот, разблокировать")
    disable.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    load_env()

    registry = OrgRegistry.load()
    if args.org_key and not registry.has(args.org_key):
        log.error(
            "Организация «%s» не найдена. Доступны: %s",
            args.org_key, ", ".join(o.key for o in registry),
        )
        return 1

    if args.token or args.org:
        selected = registry.get(args.org_key)
        directory = Directory(
            Y360Client(args.token or selected.token, args.org or selected.org_id)
        )
    else:
        directory = registry.directory(args.org_key)

    try:
        if args.command == "check":
            return cmd_check(registry, directory, args)
        if args.command == "orgs":
            return cmd_orgs(registry, directory)
        if args.command == "dept":
            return cmd_departments(directory)
        if args.command == "export":
            return cmd_export(directory, args)
        if args.command == "create":
            return cmd_create(directory, args)
        if args.command == "disable":
            return cmd_disable(directory, args)
    except ApiError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130
    return 0


def cmd_check(registry: OrgRegistry, directory: Directory, args) -> int:
    """Проверяет токен по шагам и говорит, что именно не так."""
    import requests

    org = registry.get(args.org_key)
    token = org.token if not args.token else args.token

    print("1) Формат токена")
    if not token:
        print("   ✗ токен пуст — заполните Y360_TOKEN в .env")
        return 1
    if not token.startswith("y0_"):
        print(f"   ✗ начинается с «{token[:3]}», а должен с «y0_»")
        print("     Похоже, скопирован не токен (например, ClientID или Client secret).")
    elif len(token) < 40 or any(c in token for c in "&? \"'"):
        print(f"   ✗ подозрительная строка: длина {len(token)}")
        print("     Из адресной строки нужен только кусок между access_token= и &")
    else:
        print(f"   ✓ y0_… длина {len(token)}")

    print("2) Чей это токен")
    try:
        response = requests.get(
            "https://login.yandex.ru/info?format=json",
            headers={"Authorization": f"OAuth {token}"},
            timeout=30,
        )
        if response.status_code == 401:
            print("   ✗ Яндекс не принял токен: недействителен, отозван или истёк")
            print("     Выпустите заново: oauth.yandex.ru/authorize?response_type=token&client_id=…")
            return 1
        response.raise_for_status()
        info = response.json()
        print(f"   ✓ {info.get('login')} ({info.get('display_name', '')})")
        print("     Это должен быть администратор организации.")
    except requests.RequestException as exc:
        print(f"   ? проверить не удалось: {exc}")

    print("3) Список организаций (право directory:read_organization)")
    org_ids = []
    try:
        available = directory.client.organizations()
        for item in available:
            org_ids.append(str(item.get("id")))
            print(f"   ✓ {item.get('id')}\t{item.get('name')}")
        if not available:
            print("   ? токен не привязан ни к одной организации")
    except ApiError as exc:
        print(f"   ✗ {exc}")
        print("     Права на чтение организаций нет — это не мешает работе бота.")
        print("     ID организации возьмите в админке (параметр org= в адресной строке).")

    print(f"4) Доступ к организации {org.org_id}")
    if org_ids and org.org_id not in org_ids:
        print(f"   ✗ в .env указан {org.org_id}, а токен видит: {', '.join(org_ids)}")
        print("     Впишите в Y360_ORG_ID правильный ID.")
        return 1
    try:
        departments = directory.departments(refresh=True)
        print(f"   ✓ отделов: {len(departments)}")
        users = directory.users(refresh=True)
        print(f"   ✓ сотрудников: {len(users)}")
        print("\nВсё готово: docker compose up -d --build")
        return 0
    except ApiError as exc:
        print(f"   ✗ {exc}")
        if exc.status == 401:
            print("     Токен не принят: выпустите заново под администратором организации.")
        elif exc.status == 403:
            print("     Не выданы права directory:read_users / directory:read_departments,")
            print("     либо токен получен не под администратором организации.")
        elif exc.status == 404:
            print(f"     Организации {org.org_id} не существует — проверьте Y360_ORG_ID.")
        return 1


def cmd_orgs(registry: OrgRegistry, directory: Directory) -> int:
    print("Настроенные организации:")
    for org in registry:
        domain = f"  {org.domain}" if org.domain else ""
        print(f"  {org.key}\t{org.org_id}\t{org.name}{domain}")
    try:
        available = directory.client.organizations()
    except ApiError as exc:
        print(f"\nСписок организаций из API недоступен: {exc}")
        return 0
    if available:
        print("\nДоступны по этому токену:")
        for org in available:
            print(f"  {org.get('id')}\t{org.get('name')}")
    return 0


def cmd_departments(directory: Directory) -> int:
    departments = sorted(directory.departments(), key=lambda d: int(d.get("id", 0)))
    for department in departments:
        print(
            f"{department.get('id')}\t{department.get('name')}"
            f"\t(сотрудников: {department.get('membersCount', 0)})"
        )
    return 0


def cmd_export(directory: Directory, args) -> int:
    rows = []
    for user in directory.users(refresh=True):
        name = user.get("name") or {}
        phones = [
            c.get("value")
            for c in user.get("contacts") or []
            if c.get("type") == "phone"
        ]
        rows.append({
            "id": user.get("id"),
            "last": name.get("last", ""),
            "first": name.get("first", ""),
            "middle": name.get("middle", ""),
            "nickname": user.get("nickname", ""),
            "email": user.get("email", ""),
            "aliases": " ".join(user.get("aliases") or []),
            "department_id": user.get("departmentId", ""),
            "position": user.get("position", ""),
            "phone": phones[0] if phones else "",
            "is_enabled": user.get("isEnabled", ""),
            "is_admin": user.get("isAdmin", ""),
            "is_dismissed": user.get("isDismissed", ""),
            "created_at": user.get("createdAt", ""),
        })
    path = write_rows(args.out, rows, EXPORT_FIELDS)
    print(f"Выгружено {len(rows)} сотрудников -> {path}")
    return 0


def cmd_create(directory: Directory, args) -> int:
    rows = read_rows(args.file)
    if not rows:
        log.error("В файле %s нет строк", args.file)
        return 1

    directory.taken_nicknames(refresh=True)
    results: list[dict] = []
    created_count = 0
    failed_count = 0

    for index, row in enumerate(rows, start=1):
        try:
            user = row_to_user(row)
        except ValueError as exc:
            results.append(_result_row("skipped", NewUser(first="", last=""), "", str(exc), row))
            failed_count += 1
            log.warning("Строка %s: %s", index, exc)
            continue

        try:
            department_id, department_name = directory.resolve_department(
                row.get("department") or args.department
            )
        except ValueError as exc:
            results.append(_result_row("skipped", user, "", str(exc), row))
            failed_count += 1
            log.warning("Строка %s (%s %s): %s", index, user.last, user.first, exc)
            continue

        user.department_id = department_id
        user.department_name = department_name
        user.password_change_required = not args.no_password_change
        directory.prepare(
            user,
            nickname_pattern=args.nickname_pattern,
            password_length=args.password_length,
        )

        if args.dry_run:
            print(
                f"[dry-run] {user.last} {user.first} -> {user.nickname} "
                f"(отдел: {department_name or department_id})"
            )
            results.append(_result_row("dry-run", user, "", "", row))
            continue

        try:
            created = directory.create(user)
        except ApiError as exc:
            failed_count += 1
            log.error("Строка %s (%s %s): %s", index, user.last, user.first, exc)
            results.append(_result_row("error", user, "", str(exc), row))
            continue

        created_count += 1
        email = created.get("email", user.nickname)
        print(f"OK  {user.last} {user.first} -> {email}")
        results.append(
            _result_row(
                "created", user, email,
                "; ".join(created.get("_alias_errors", [])), row,
            )
        )
        time.sleep(max(args.delay, 0))

    out = args.out or f"out/created_{datetime.now():%Y%m%d_%H%M%S}.csv"
    path = write_rows(out, results, CREATE_RESULT_FIELDS)
    print(
        f"\nСоздано: {created_count}, ошибок/пропущено: {failed_count}. "
        f"Результат (с паролями): {path}"
    )
    return 0 if failed_count == 0 else 2


def cmd_disable(directory: Directory, args) -> int:
    targets = list(args.targets)
    if args.file:
        for row in read_rows(args.file):
            value = row.get("nickname") or row.get("id") or ""
            if value:
                targets.append(value)
    if not targets:
        log.error("Не указаны сотрудники: перечислите логины или задайте -f файл")
        return 1

    by_key: dict[str, dict] = {}
    for user in directory.users(refresh=True):
        by_key[str(user.get("id"))] = user
        by_key[(user.get("nickname") or "").lower()] = user
        by_key[(user.get("email") or "").lower()] = user

    failed = 0
    for target in targets:
        user = by_key.get(target.strip().lower()) or by_key.get(target.strip())
        if not user:
            log.warning("Не найден: %s", target)
            failed += 1
            continue
        action = "разблокирую" if args.enable else "блокирую"
        if args.dry_run:
            print(f"[dry-run] {action} {user.get('email')}")
            continue
        try:
            directory.client.patch_user(user["id"], {"isEnabled": bool(args.enable)})
            print(f"OK  {action} {user.get('email')}")
        except ApiError as exc:
            log.error("%s: %s", target, exc)
            failed += 1
    return 0 if failed == 0 else 2


def row_to_user(row: dict) -> NewUser:
    first = row.get("first", "").strip()
    last = row.get("last", "").strip()
    if not first or not last:
        raise ValueError("нужны имя и фамилия")

    aliases_raw = row.get("aliases", "")
    aliases = [a for a in aliases_raw.replace(";", " ").replace(",", " ").split() if a]

    nickname = row.get("nickname", "").strip()
    if "@" in nickname:
        nickname = nickname.split("@", 1)[0]

    return NewUser(
        first=first,
        last=last,
        middle=row.get("middle", "").strip(),
        nickname=nickname,
        password=row.get("password", "").strip(),
        department_name=row.get("department", "").strip(),
        position=row.get("position", "").strip(),
        phone=row.get("phone", "").strip(),
        phone_extension=row.get("phone_extension", "").strip(),
        birthday=row.get("birthday", "").strip(),
        gender=GENDER_ALIASES.get(row.get("gender", "").strip().lower(), ""),
        about=row.get("about", "").strip(),
        display_name=row.get("display_name", "").strip(),
        external_id=row.get("external_id", "").strip(),
        aliases=aliases,
    )


def _result_row(status: str, user: NewUser, email: str, error: str, row: dict) -> dict:
    return {
        "status": status,
        "last": user.last or row.get("last", ""),
        "first": user.first or row.get("first", ""),
        "middle": user.middle,
        "nickname": user.nickname,
        "email": email,
        "password": user.password if status in {"created", "dry-run"} else "",
        "department": user.department_name or user.department_id,
        "position": user.position,
        "error": error,
    }


if __name__ == "__main__":
    sys.exit(main())
