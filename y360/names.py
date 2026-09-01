import re
import secrets
import unicodedata

# Паспортный вариант (загранпаспорт РФ): Хан -> Khan, Юлия -> Iuliia.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
    # украинские/белорусские буквы, чтобы не падать на них
    "і": "i", "ї": "i", "є": "e", "ґ": "g", "ў": "u",
}

ALPHABET_LOWER = "abcdefghijkmnopqrstuvwxyz"   # без l
ALPHABET_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"    # без I и O
DIGITS = "23456789"                            # без 0 и 1
SPECIALS = "!@#$%*-_=+"


# Привычный «айтишный» вариант: Хан -> Han, Юлия -> Yuliya.
# Им обычно заведены почты, сделанные руками.
TRANSLIT_SIMPLE = {
    **TRANSLIT,
    "х": "h", "ц": "c", "щ": "sch", "ы": "y", "й": "y",
    "ю": "yu", "я": "ya", "ё": "yo", "э": "e",
}

STYLES = {"passport": TRANSLIT, "simple": TRANSLIT_SIMPLE}


def translit(text: str, style: str = "passport") -> str:
    table = STYLES.get(style, TRANSLIT)
    result = []
    for char in (text or "").strip().lower():
        if char in table:
            result.append(table[char])
        elif char.isascii() and char.isalnum():
            result.append(char)
        elif char in " -_.":
            result.append(".")
        else:
            # латиница с диакритикой -> базовая буква
            decomposed = unicodedata.normalize("NFKD", char)
            stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
            if stripped.isascii() and stripped.isalnum():
                result.append(stripped.lower())
    slug = "".join(result)
    slug = re.sub(r"\.{2,}", ".", slug).strip(".")
    return slug


def make_nickname(
    first: str, last: str, pattern: str = "{first}.{last}", style: str = "passport"
) -> str:
    """Логин из имени/фамилии по шаблону: {first} {last} {f} {l}."""
    first_slug = translit(first, style)
    last_slug = translit(last, style)
    nickname = pattern.format(
        first=first_slug,
        last=last_slug,
        f=first_slug[:1],
        l=last_slug[:1],
    )
    nickname = re.sub(r"[^a-z0-9._-]", "", nickname.lower()).strip("._-")
    return nickname[:30] or "user"


def unique_nickname(base: str, taken: set[str]) -> str:
    """Добавляет цифровой суффикс, если логин уже занят."""
    if base not in taken:
        return base
    for index in range(2, 100):
        candidate = f"{base}{index}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"Не удалось подобрать свободный логин для {base}")


def generate_password(length: int = 14) -> str:
    """Пароль, гарантированно содержащий строчную, прописную, цифру и спецсимвол."""
    length = max(10, min(length, 20))  # требования Яндекс 360: до 20 символов
    pools = [ALPHABET_LOWER, ALPHABET_UPPER, DIGITS, SPECIALS]
    chars = [secrets.choice(pool) for pool in pools]
    all_chars = "".join(pools)
    chars += [secrets.choice(all_chars) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
