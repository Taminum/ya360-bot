import os
from pathlib import Path


def load_env(path: str = ".env") -> None:
    """Простой .env-парсер: KEY=VALUE, комментарии с #, кавычки снимаются.

    Переменные окружения имеют приоритет над файлом.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require(name: str, cli_value: str | None = None) -> str:
    value = cli_value or os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Не задано {name}. Укажите его в .env (см. .env.example) "
            f"или переменной окружения."
        )
    return value
