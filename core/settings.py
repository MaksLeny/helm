"""
Helm — пользовательские настройки, сохраняемые между перезапусками.

config.py задаёт ДЕФОЛТЫ (и читает env). Этот модуль кладёт поверх файл
helm_settings.json в корне проекта: всё, что пользователь меняет из веб-
интерфейса (порт, TTL сессии, хеш пароля), пишется сюда и переживает рестарт.

Приоритет значений: env-переменная > файл настроек > дефолт из config.py.
(env оставляем главным, чтобы запуск с HELM_PASSWORD_HASH=... всегда работал.)

Порт и TTL читаются при старте сервера. Смена пароля применяется сразу
(в рантайме обновляется хеш), порт/TTL — после перезапуска.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("helm")

_FILE = Path(__file__).parent.parent / "helm_settings.json"

# Ключи и их валидаторы/типы
_DEFAULTS: dict[str, Any] = {}   # заполняется из config при первом обращении


def _load_raw() -> dict:
    if not _FILE.exists():
        return {}
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Настройки: не удалось прочитать %s: %s", _FILE, e)
        return {}


def _save_raw(data: dict) -> bool:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Настройки: не удалось записать %s: %s", _FILE, e)
        return False


def get(key: str, default: Any = None) -> Any:
    """Прочитать настройку: env > файл > переданный default."""
    env_key = "HELM_" + key.upper()
    if env_key in os.environ:
        return os.environ[env_key]
    data = _load_raw()
    return data.get(key, default)


def set_many(values: dict) -> bool:
    """Записать несколько настроек в файл (слиянием с существующими)."""
    data = _load_raw()
    data.update(values)
    return _save_raw(data)


# --- типизированные геттеры для ядра ----------------------------------------

def get_port(default: int) -> int:
    raw = get("port", default)
    try:
        p = int(raw)
        if 1 <= p <= 65535:
            return p
    except (TypeError, ValueError):
        pass
    return default


def get_session_ttl(default: float) -> float:
    raw = get("session_ttl", default)
    try:
        t = float(raw)
        # Разумные пределы: от 5 минут до 90 дней.
        if 300 <= t <= 90 * 24 * 3600:
            return t
    except (TypeError, ValueError):
        pass
    return default


def get_password_hash(default: str) -> str:
    # env имеет наивысший приоритет (см. get), затем файл, затем дефолт.
    val = get("password_hash", None)
    return val if val else default


def settings_file_path() -> str:
    return str(_FILE)


# --- гостевые разделы --------------------------------------------------------

# Какие плагины по умолчанию доступны гостю (без админ-пароля).
DEFAULT_GUEST_PLUGINS = ["media", "monitor", "clipboard"]


def get_guest_plugins() -> list[str]:
    """Список id плагинов, доступных гостю."""
    raw = get("guest_plugins", None)
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return list(DEFAULT_GUEST_PLUGINS)


def set_guest_plugins(plugin_ids: list[str]) -> bool:
    return set_many({"guest_plugins": list(plugin_ids)})


# --- избранные приложения ----------------------------------------------------

def get_favorite_apps() -> list[str]:
    """Список id приложений, отмеченных избранными (для быстрого запуска)."""
    raw = get("favorite_apps", None)
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def set_favorite_apps(app_ids: list[str]) -> bool:
    return set_many({"favorite_apps": list(app_ids)})
