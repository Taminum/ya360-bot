import json
import logging
import time
from typing import Any, Iterator

import requests

log = logging.getLogger("y360")

BASE_URL = "https://api360.yandex.net"


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: Any):
        self.status = status
        self.body = body
        message = body
        if isinstance(body, dict):
            message = body.get("message") or body.get("error_description") or body
        super().__init__(f"{method} {url} -> HTTP {status}: {message}")


class Y360Client:
    """Тонкий клиент Directory API 360 с ретраями и пагинацией."""

    def __init__(
        self,
        token: str,
        org_id: str | int | None = None,
        timeout: int = 30,
        max_retries: int = 5,
    ):
        self.org_id = str(org_id) if org_id is not None else None
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"OAuth {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # --- низкий уровень -------------------------------------------------
    def request(
        self, method: str, path: str, retries: int | None = None, **kwargs
    ) -> Any:
        url = BASE_URL + path
        delay = 1.0
        last_error: Exception | None = None
        attempts = retries or self.max_retries

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:  # сеть моргнула
                last_error = exc
                log.warning("Сетевая ошибка (%s/%s): %s", attempt, attempts, exc)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                log.warning(
                    "HTTP %s от API, повтор через %.0f c (%s/%s)",
                    response.status_code,
                    wait,
                    attempt,
                    attempts,
                )
                last_error = ApiError(method, url, response.status_code, _body(response))
                time.sleep(wait)
                delay = min(delay * 2, 30)
                continue

            if not response.ok:
                raise ApiError(method, url, response.status_code, _body(response))

            return _body(response)

        raise last_error if last_error else RuntimeError("Не удалось выполнить запрос")

    def paginate(self, path: str, key: str, per_page: int = 100) -> Iterator[dict]:
        """Идёт по всем страницам списочного метода и отдаёт элементы."""
        page = 1
        while True:
            data = self.request(
                "GET", path, params={"page": page, "perPage": per_page}
            )
            items = data.get(key, [])
            for item in items:
                yield item
            pages = data.get("pages", 1)
            if page >= pages or not items:
                return
            page += 1

    def paginate_token(
        self, path: str, key: str, params: dict, max_pages: int = 500,
        retries: int | None = None,
    ) -> Iterator[dict]:
        """Пагинация через nextPageToken — так устроены аудит-логи."""
        token = None
        for _ in range(max_pages):
            query = dict(params)
            if token:
                query["pageToken"] = token
            data = self.request("GET", path, params=query, retries=retries)
            for item in data.get(key, []):
                yield item
            token = data.get("nextPageToken")
            if not token:
                return
        log.warning("Достигнут предел в %s страниц для %s", max_pages, path)

    # --- аудит-лог ------------------------------------------------------
    @property
    def audit_mail_path(self) -> str:
        return f"/security/v1/org/{self._org()}/audit_log/mail"

    def audit_log_mail(
        self, params: dict, max_pages: int = 500, retries: int | None = None
    ) -> Iterator[dict]:
        """События Почты. Нужно право ya360_security:audit_log_mail."""
        return self.paginate_token(
            self.audit_mail_path, "events", params, max_pages, retries
        )

    # --- организации ----------------------------------------------------
    def organizations(self) -> list[dict]:
        data = self.request("GET", "/directory/v1/org", params={"perPage": 100})
        return data.get("organizations", [])

    def _org(self) -> str:
        if not self.org_id:
            raise SystemExit("Не задан Y360_ORG_ID (посмотрите командой `orgs`).")
        return self.org_id

    # --- отделы ---------------------------------------------------------
    def departments(self) -> list[dict]:
        return list(
            self.paginate(
                f"/directory/v1/org/{self._org()}/departments", "departments"
            )
        )

    # --- сотрудники -----------------------------------------------------
    def users(self) -> list[dict]:
        return list(self.paginate(f"/directory/v1/org/{self._org()}/users", "users"))

    def create_user(self, payload: dict) -> dict:
        return self.request(
            "POST",
            f"/directory/v1/org/{self._org()}/users",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def patch_user(self, user_id: str, payload: dict) -> dict:
        return self.request(
            "PATCH",
            f"/directory/v1/org/{self._org()}/users/{user_id}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def get_user(self, user_id: str) -> dict:
        return self.request("GET", f"/directory/v1/org/{self._org()}/users/{user_id}")

    def add_alias(self, user_id: str, alias: str) -> dict:
        return self.request(
            "POST",
            f"/directory/v1/org/{self._org()}/users/{user_id}/aliases",
            data=json.dumps({"alias": alias}, ensure_ascii=False).encode("utf-8"),
        )

    def delete_alias(self, user_id: str, alias: str) -> dict:
        return self.request(
            "DELETE",
            f"/directory/v1/org/{self._org()}/users/{user_id}/aliases/{alias}",
        )

    def delete_user(self, user_id: str) -> dict:
        """Необратимо: удаляет сотрудника вместе с почтой и Диском."""
        return self.request(
            "DELETE", f"/directory/v1/org/{self._org()}/users/{user_id}"
        )

    def set_enabled(self, user_id: str, enabled: bool) -> dict:
        return self.patch_user(user_id, {"isEnabled": bool(enabled)})

    def set_password(
        self, user_id: str, password: str, change_required: bool = True
    ) -> dict:
        return self.patch_user(
            user_id,
            {"password": password, "passwordChangeRequired": bool(change_required)},
        )


def _body(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
