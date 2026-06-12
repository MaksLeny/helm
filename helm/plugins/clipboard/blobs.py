"""
Helm · буфер обмена — хранилище переданных файлов и картинок (на диске).

История в store.py держит только метаданные; сами байты файлов/картинок
лежат в ПОСТОЯННОЙ папке рядом с проектом и отдаются по HTTP. Папка переживает
перезагрузку ПК (постоянная память буфера), имеет лимит по числу файлов.

Идентификатор blob'а — случайный токен, путь не угадать и нельзя выйти за
пределы папки (защита от path traversal: имя только из hex-токена).
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

log = logging.getLogger("helm")

# Постоянная папка рядом с проектом (не temp!) — переживает перезагрузку.
_DIR = Path(__file__).parent.parent.parent / "helm_clipboard_store"
_MAX_FILES = 60                       # сколько blob'ов держим максимум
_MAX_BYTES = 50 * 1024 * 1024         # лимит на один файл — 50 МБ


def _ensure_dir() -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось создать папку буфера: %s", e)


def cleanup_all() -> None:
    """Удалить все blob'ы (вызывается только при явной очистке буфера)."""
    try:
        if _DIR.exists():
            for f in _DIR.iterdir():
                try:
                    f.unlink()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    _ensure_dir()


def delete_blob(blob_id: str) -> None:
    """Удалить один blob по id (при удалении записи из истории)."""
    p = blob_path(blob_id)
    if p:
        try:
            p.unlink()
        except Exception:  # noqa: BLE001
            pass


def _prune() -> None:
    """Оставить не более _MAX_FILES самых свежих файлов."""
    try:
        files = sorted(_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
        while len(files) > _MAX_FILES:
            old = files.pop(0)
            try:
                old.unlink()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def save_blob(data: bytes, ext: str = "") -> Optional[str]:
    """Сохранить байты, вернуть blob_id (hex-токен[.ext]) или None."""
    if data is None or len(data) > _MAX_BYTES:
        return None
    _ensure_dir()
    token = secrets.token_hex(16)
    # Расширение чистим до букв/цифр, максимум 8 символов.
    safe_ext = "".join(ch for ch in ext.lstrip(".") if ch.isalnum())[:8].lower()
    blob_id = f"{token}.{safe_ext}" if safe_ext else token
    try:
        (_DIR / blob_id).write_bytes(data)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось сохранить blob: %s", e)
        return None
    _prune()
    return blob_id


def blob_path(blob_id: str) -> Optional[Path]:
    """Безопасно получить путь к blob'у по id (без path traversal)."""
    if not blob_id:
        return None
    # Разрешаем только hex-токен и необязательное расширение из букв/цифр.
    name = os.path.basename(blob_id)         # отсекаем любые / \ ..
    if name != blob_id:
        return None
    stem = name.split(".", 1)[0]
    if not stem or any(c not in "0123456789abcdef" for c in stem):
        return None
    p = _DIR / name
    try:
        if p.exists() and p.is_file():
            return p
    except Exception:  # noqa: BLE001
        return None
    return None


def blob_size(blob_id: str) -> int:
    p = blob_path(blob_id)
    try:
        return p.stat().st_size if p else 0
    except Exception:  # noqa: BLE001
        return 0
