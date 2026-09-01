import csv
from pathlib import Path

# Русские и английские заголовки -> внутренние имена полей.
HEADER_ALIASES = {
    "фамилия": "last", "last": "last", "lastname": "last", "last_name": "last", "surname": "last",
    "имя": "first", "first": "first", "firstname": "first", "first_name": "first", "name": "first",
    "отчество": "middle", "middle": "middle", "middlename": "middle", "middle_name": "middle",
    "логин": "nickname", "nickname": "nickname", "login": "nickname", "алиас": "aliases",
    "почта": "nickname", "email": "nickname", "mail": "nickname", "адрес": "nickname",
    "пароль": "password", "password": "password", "pass": "password",
    "отдел": "department", "department": "department", "подразделение": "department",
    "departmentid": "department", "department_id": "department", "отдел_id": "department",
    "должность": "position", "position": "position", "должность_": "position",
    "телефон": "phone", "phone": "phone", "тел": "phone", "мобильный": "phone",
    "добавочный": "phone_extension", "extension": "phone_extension",
    "днр": "birthday", "деньрождения": "birthday", "birthday": "birthday",
    "дата_рождения": "birthday", "датарождения": "birthday", "др": "birthday",
    "пол": "gender", "gender": "gender",
    "о_себе": "about", "осебе": "about", "about": "about", "комментарий": "about",
    "алиасы": "aliases", "aliases": "aliases", "alias": "aliases",
    "внешний_id": "external_id", "external_id": "external_id", "externalid": "external_id",
    "табельный": "external_id", "табельный_номер": "external_id",
    "отображаемое_имя": "display_name", "display_name": "display_name",
    "displayname": "display_name", "отображаемоеимя": "display_name",
    "id": "id", "userid": "id", "user_id": "id",
}

GENDER_ALIASES = {
    "м": "male", "муж": "male", "мужской": "male", "male": "male", "m": "male",
    "ж": "female", "жен": "female", "женский": "female", "female": "female", "f": "female",
}


def _normalize_header(header: str) -> str:
    key = (header or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = key.lstrip("\ufeff")
    return HEADER_ALIASES.get(key, HEADER_ALIASES.get(key.replace("_", ""), key))


def read_rows(path: str | Path) -> list[dict]:
    """Читает CSV (запятая/точка с запятой/таб) с русскими или английскими шапками."""
    text = Path(path).read_text(encoding="utf-8-sig")
    if not text.strip():
        return []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","

    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    rows = []
    for raw in reader:
        row = {}
        for key, value in raw.items():
            if key is None:
                continue
            row[_normalize_header(key)] = (value or "").strip()
        if any(row.values()):
            rows.append(row)
    return rows


def write_rows(path: str | Path, rows: list[dict], fieldnames: list[str]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig + ';' — чтобы Excel открывал без плясок с кодировкой
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return path
