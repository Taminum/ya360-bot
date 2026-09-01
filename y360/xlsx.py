"""Выгрузка сотрудников в .xlsx — с шапкой, фильтром и нормальной шириной колонок."""

from pathlib import Path

# Человеческие названия колонок для Excel.
TITLES = {
    "id": "ID",
    "last": "Фамилия",
    "first": "Имя",
    "middle": "Отчество",
    "nickname": "Логин",
    "email": "Почта",
    "password": "Пароль",
    "aliases": "Алиасы",
    "department_id": "Отдел (ID)",
    "department": "Отдел",
    "position": "Должность",
    "phone": "Телефон",
    "is_enabled": "Активен",
    "is_admin": "Админ",
    "is_dismissed": "Уволен",
    "created_at": "Создан",
}

BOOL_WORDS = {True: "да", False: "нет", "True": "да", "False": "нет"}


def available() -> bool:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def write_users(path: str | Path, rows: list[dict], fields: list[str]) -> Path:
    """Пишет таблицу сотрудников. Требует openpyxl."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    book = Workbook()
    sheet = book.active
    sheet.title = "Сотрудники"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", start_color="4C6EF5")
    sheet.append([TITLES.get(field, field) for field in fields])
    for cell in sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")

    for row in rows:
        sheet.append([_cell(row.get(field, "")) for field in fields])

    widths = [len(TITLES.get(field, field)) + 2 for field in fields]
    for row in rows:
        for index, field in enumerate(fields):
            widths[index] = max(widths[index], min(len(_cell(row.get(field, ""))) + 2, 42))
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    book.save(path)
    return path


def _cell(value) -> str:
    if isinstance(value, bool) or value in BOOL_WORDS:
        return BOOL_WORDS.get(value, str(value))
    return "" if value is None else str(value)
