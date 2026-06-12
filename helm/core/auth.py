"""
Helm — аутентификация.

Пароль проверяется по scrypt-хешу (только stdlib, без зависимостей).
После успешного входа выдаётся случайный токен сессии, который живёт в памяти
сервера с TTL. Токен телефон хранит в cookie и присылает при каждом запросе.

Формат хранимого хеша: "salt_hex$hash_hex".
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time

from .config import PASSWORD_HASH, SESSION_TTL

log = logging.getLogger("helm")

# Текущий хеш пароля. Изначально из config (env > файл > дефолт), но может быть
# изменён в рантайме через set_password_hash() при смене пароля из интерфейса.
_current_hash: str = PASSWORD_HASH

# Параметры scrypt. Подобраны как разумный компромисс «быстро на ПК, дорого
# для перебора». N должно быть степенью двойки.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


def set_password_hash(new_hash: str) -> None:
    """Обновить активный хеш пароля в рантайме (после смены пароля)."""
    global _current_hash
    _current_hash = new_hash


def hash_password(password: str, *, salt_hex: str | None = None) -> str:
    """Посчитать строку хеша 'salt$hash' для заданного пароля."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN,
    )
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str) -> bool:
    """Сверить введённый пароль с активным хешем (защита от timing-атак)."""
    try:
        salt_hex, expected_hex = _current_hash.split("$", 1)
        candidate = hash_password(password, salt_hex=salt_hex)
        _, candidate_hex = candidate.split("$", 1)
        return secrets.compare_digest(candidate_hex, expected_hex)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка проверки пароля (некорректный формат хеша?)")
        return False


# --------------------------------------------------------------------------- #
#                         ХРАНИЛИЩЕ СЕССИЙ (в памяти)                          #
# --------------------------------------------------------------------------- #

class SessionStore:
    """Хранилище токенов сессий в памяти с TTL, ролью и метаданными.

    Значение по токену: словарь {expires, role, name, created, seen}.
      name    — имя, которое гость вводит сам (или None);
      created — когда сессия создана (wall-clock, для отображения);
      seen    — последняя активность (для «кто онлайн»).
    """

    def __init__(self) -> None:
        self._tokens: dict[str, dict] = {}

    def create(self, role: str = "guest", name: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._tokens[token] = {
            "expires": time.monotonic() + SESSION_TTL,
            "role": role,
            "name": name,
            "created": now,
            "seen": now,
        }
        self._sweep()
        return token

    def _entry(self, token: str | None) -> dict | None:
        if not token:
            return None
        e = self._tokens.get(token)
        if e is None:
            return None
        if time.monotonic() > e["expires"]:
            self._tokens.pop(token, None)
            return None
        return e

    def valid(self, token: str | None) -> bool:
        return self._entry(token) is not None

    def role(self, token: str | None) -> str | None:
        e = self._entry(token)
        return e["role"] if e else None

    def is_admin(self, token: str | None) -> bool:
        return self.role(token) == "admin"

    def name(self, token: str | None) -> str | None:
        e = self._entry(token)
        return e.get("name") if e else None

    def set_name(self, token: str | None, name: str) -> bool:
        e = self._entry(token)
        if e is None:
            return False
        e["name"] = (name or "").strip()[:40] or None
        return True

    def touch(self, token: str | None) -> None:
        """Отметить активность сессии (для «кто онлайн»)."""
        e = self._entry(token)
        if e is not None:
            e["seen"] = time.time()

    def online(self, within_seconds: float = 30.0) -> list[dict]:
        """Список недавно активных сессий: [{name, role, created, seen, idx}]."""
        now = time.time()
        out = []
        for i, e in enumerate(self._tokens.values()):
            if time.monotonic() <= e["expires"] and (now - e["seen"]) <= within_seconds:
                out.append({
                    "name": e.get("name"),
                    "role": e["role"],
                    "created": e["created"],
                    "seen": e["seen"],
                })
        return out

    def revoke(self, token: str | None) -> None:
        if token:
            self._tokens.pop(token, None)

    def _sweep(self) -> None:
        now = time.monotonic()
        dead = [t for t, e in self._tokens.items() if now > e["expires"]]
        for t in dead:
            self._tokens.pop(t, None)


sessions = SessionStore()
