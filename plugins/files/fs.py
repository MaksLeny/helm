r"""
Helm · файлы — безопасный доступ к файловой системе ПК.

КЛЮЧЕВОЕ ПО БЕЗОПАСНОСТИ
Любой путь, пришедший с телефона, проходит через _resolve():
  • abs-путь нормализуется (resolve), символические ссылки раскрываются;
  • проверяется, что итог лежит ВНУТРИ одного из разрешённых корней (roots);
  • любые "..", попытки выйти за корень, несуществующие/недоступные пути —
    отвергаются (возвращается None).
Корни — это диски (C:\, D:\ …) и домашняя папка. За их пределы выйти нельзя.

Опасные системные каталоги (Windows, /proc, /sys и т.п.) скрываются при
листинге, чтобы случайно не лазить по системе.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import string
from pathlib import Path
from typing import Optional

log = logging.getLogger("helm")

_IS_WIN = platform.system() == "Windows"

# Каталоги, которые прячем из листинга и закрываем для скачивания
# (по началу полного пути, нижний регистр).
_HIDE_WIN = (
    "c:\\windows\\system32", "c:\\windows\\syswow64", "c:\\$recycle.bin",
    "c:\\windows\\winsxs", "c:\\programdata\\microsoft\\windows",
)
_HIDE_NIX = (
    "/proc", "/sys", "/dev", "/etc/shadow", "/etc/ssh", "/root/.ssh",
    "/boot", "/run",
)


def list_roots() -> list[dict]:
    """Список корней навигации: диски (Windows) + домашняя папка."""
    roots: list[dict] = []
    home = Path.home()
    roots.append({"name": "Домашняя папка", "path": str(home), "kind": "home"})
    if _IS_WIN:
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                roots.append({"name": f"Диск {letter}:", "path": drive, "kind": "drive"})
    else:
        roots.append({"name": "Корень /", "path": "/", "kind": "drive"})
    return roots


def _allowed_roots() -> list[Path]:
    """Разрешённые корни как нормализованные Path."""
    out: list[Path] = []
    for r in list_roots():
        try:
            out.append(Path(r["path"]).resolve())
        except Exception:  # noqa: BLE001
            pass
    return out


def _within_roots(p: Path) -> bool:
    """Лежит ли путь внутри одного из разрешённых корней."""
    for root in _allowed_roots():
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve(raw: str) -> Optional[Path]:
    """Безопасно превратить присланный путь в Path внутри разрешённых корней.

    Возвращает None при любой попытке выйти за пределы / некорректном пути.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        p = Path(raw).resolve()
    except Exception:  # noqa: BLE001
        return None
    if not _within_roots(p):
        log.warning("Файлы: путь вне разрешённых корней отклонён: %r", raw)
        return None
    return p


def _is_hidden_system(p: Path) -> bool:
    low = str(p).lower()
    hides = _HIDE_WIN if _IS_WIN else _HIDE_NIX
    return any(low.startswith(h) for h in hides)


def _fmt_entry(p: Path) -> Optional[dict]:
    """Метаданные одного элемента папки (или None, если недоступен)."""
    try:
        st = p.stat()
    except Exception:  # noqa: BLE001
        return None
    is_dir = stat.S_ISDIR(st.st_mode)
    return {
        "name": p.name,
        "path": str(p),
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "mtime": st.st_mtime,
    }


def list_dir(raw_path: str) -> Optional[dict]:
    """Содержимое папки: {path, parent, entries[]} или None при отказе."""
    p = _resolve(raw_path)
    if p is None or not p.is_dir():
        return None
    if _is_hidden_system(p):
        return None
    entries: list[dict] = []
    try:
        for child in p.iterdir():
            if _is_hidden_system(child):
                continue
            # Прячем скрытые файлы (точка в начале) — но папки-корни видны.
            if child.name.startswith(".") and not _IS_WIN:
                continue
            e = _fmt_entry(child)
            if e is not None:
                entries.append(e)
    except PermissionError:
        return {"path": str(p), "parent": _parent_of(p), "entries": [],
                "error": "нет доступа к папке"}
    except Exception as e:  # noqa: BLE001
        log.debug("list_dir error %s: %s", p, e)
        return None
    # Папки сверху, потом по имени без учёта регистра.
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"path": str(p), "parent": _parent_of(p), "entries": entries}


def _parent_of(p: Path) -> Optional[str]:
    """Родительская папка, если она в пределах корней; иначе None."""
    parent = p.parent
    if parent == p:                 # уже корень диска
        return None
    if not _within_roots(parent):
        return None
    return str(parent)


def file_for_download(raw_path: str) -> Optional[Path]:
    """Проверить путь и вернуть Path файла для отдачи, либо None."""
    p = _resolve(raw_path)
    if p is None or not p.is_file():
        return None
    if _is_hidden_system(p):
        return None
    return p


def save_upload(dir_path: str, filename: str, data: bytes) -> Optional[str]:
    """Сохранить присланный файл в указанную папку. Возвращает путь или None."""
    d = _resolve(dir_path)
    if d is None or not d.is_dir():
        return None
    # Имя файла очищаем от любых разделителей — кладём строго в эту папку.
    safe_name = os.path.basename(filename or "file").strip() or "file"
    target = (d / safe_name)
    # Финальная проверка: цель всё ещё внутри корней.
    try:
        target_resolved = target.resolve()
    except Exception:  # noqa: BLE001
        return None
    if not _within_roots(target_resolved):
        return None
    try:
        target.write_bytes(data)
    except Exception as e:  # noqa: BLE001
        log.warning("Файлы: не удалось сохранить %s: %s", target, e)
        return None
    return str(target)


def delete_path(raw_path: str) -> tuple[bool, str]:
    """Удалить файл или (пустую/непустую) папку. (ok, сообщение)."""
    p = _resolve(raw_path)
    if p is None:
        return False, "путь недоступен"
    # Запрещаем удалять сам корень диска/домашнюю папку.
    if p in _allowed_roots():
        return False, "нельзя удалить корневую папку"
    if _is_hidden_system(p):
        return False, "системный путь защищён"
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return True, "удалено"
    except PermissionError:
        return False, "нет прав на удаление"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def open_on_pc(raw_path: str) -> tuple[bool, str]:
    """Открыть файл на ПК в программе по умолчанию. (ok, сообщение).

    Безопасность: путь проходит ту же проверку _resolve (внутри разрешённых
    корней) и не должен быть системным. Открываем средствами ОС:
      Windows — os.startfile; Linux — xdg-open; macOS — open.
    Папки тоже можно открыть (в проводнике).
    """
    p = _resolve(raw_path)
    if p is None or not p.exists():
        return False, "путь недоступен"
    if _is_hidden_system(p):
        return False, "системный путь защищён"
    try:
        if _IS_WIN:
            os.startfile(str(p))  # type: ignore[attr-defined]  # noqa: S606
        elif platform.system() == "Darwin":
            import subprocess
            subprocess.Popen(["open", str(p)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(p)])
        return True, "открыто на ПК"
    except Exception as e:  # noqa: BLE001
        log.warning("Файлы: не удалось открыть %s: %s", p, e)
        return False, f"не удалось открыть: {e}"
