"""
Запуск приложений из белого списка.

Безопасность: на вход принимаем только id. Путь/команду берём из apps_config,
а не из запроса. Если id нет в списке — отказ. Так подменить цель с телефона
невозможно.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser

from .apps_config import APPS

log = logging.getLogger("helm")

_IS_WIN = sys.platform.startswith("win")
_NO_WINDOW = 0x08000000 if _IS_WIN else 0

try:
    import psutil
    _PSUTIL = True
except Exception:  # noqa: BLE001
    _PSUTIL = False

# id -> запись, для быстрого поиска
_BY_ID = {a["id"]: a for a in APPS}


def _proc_names(app: dict) -> set[str]:
    """Возможные имена процесса для записи белого списка (в нижнем регистре).

    URL-записи процессов не имеют (открываются в браузере) → пустое множество.
    Имя берём из target: basename пути или само имя команды; на Windows
    добавляем вариант с .exe. Можно переопределить полем "proc" в apps_config.
    """
    target = app.get("target", "")
    if not isinstance(target, str) or target.startswith(("http://", "https://")):
        return set()
    names: set[str] = set()
    # Явное переопределение из конфига (если пользователь задал "proc").
    explicit = app.get("proc")
    if explicit:
        vals = explicit if isinstance(explicit, (list, tuple)) else [explicit]
        for v in vals:
            names.add(str(v).lower())
    base = target.strip().strip('"')
    # Берём последний сегмент по ЛЮБОМУ разделителю (/ или \), чтобы корректно
    # работать с Windows-путями даже когда сервер не на Windows (тесты), и
    # наоборот. os.path.basename зависит от ОС, поэтому делаем вручную.
    base = base.replace("\\", "/").rstrip("/").split("/")[-1].lower()
    if base:
        names.add(base)
        if _IS_WIN and not base.endswith(".exe"):
            names.add(base + ".exe")
    return names


def _running_names() -> set[str]:
    """Множество имён всех запущенных процессов (нижний регистр)."""
    if not _PSUTIL:
        return set()
    names: set[str] = set()
    try:
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n:
                names.add(n)
    except Exception:  # noqa: BLE001
        pass
    return names


def list_apps() -> list[dict]:
    """Список для меню: id/name/icon + признаки closable/running/favorite.

    closable — у записи есть имя процесса (не URL), т.е. её можно закрыть.
    running  — процесс с таким именем сейчас запущен.
    favorite — приложение отмечено избранным (для быстрого запуска).
    """
    from core import settings
    favs = set(settings.get_favorite_apps())
    running = _running_names()
    out = []
    for a in APPS:
        pnames = _proc_names(a)
        out.append({
            "id": a["id"],
            "name": a["name"],
            "icon": a.get("icon", "▣"),
            "closable": bool(pnames),
            "running": bool(pnames & running),
            "favorite": a["id"] in favs,
        })
    return out


def toggle_favorite(app_id: str) -> tuple[bool, bool]:
    """Переключить избранное у приложения. Возвращает (ok, теперь_избранное)."""
    from core import settings
    valid_ids = {a["id"] for a in APPS}
    if app_id not in valid_ids:
        return False, False
    favs = settings.get_favorite_apps()
    if app_id in favs:
        favs = [x for x in favs if x != app_id]
        now_fav = False
    else:
        favs = favs + [app_id]
        now_fav = True
    settings.set_favorite_apps(favs)
    return True, now_fav


def close(app_id: str) -> tuple[bool, str]:
    """Закрыть приложение из белого списка по id. Вернуть (успех, текст).

    Безопасность: завершаем только процессы, чьё имя соответствует записи
    белого списка. Подменить цель с телефона нельзя — id ищется в _BY_ID,
    имя процесса вычисляется на сервере из конфига.
    """
    app = _BY_ID.get(app_id)
    if app is None:
        return False, "Приложение не в списке"
    if not _PSUTIL:
        return False, "psutil недоступен"
    pnames = _proc_names(app)
    if not pnames:
        return False, "Нечего закрывать (это ссылка)"
    killed = 0
    errors = 0
    try:
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if (p.info.get("name") or "").lower() in pnames:
                    p.terminate()
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                errors += 1
            except Exception:  # noqa: BLE001
                errors += 1
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if killed == 0:
        return False, "Приложение не запущено" if errors == 0 else "Нет прав закрыть"
    # Дать процессам завершиться, упорных — добить.
    try:
        victims = [p for p in psutil.process_iter(["name"])
                   if (p.info.get("name") or "").lower() in pnames]
        _, alive = psutil.wait_procs(victims, timeout=2.0)
        for p in alive:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return True, f"закрыто процессов: {killed}"


def launch(app_id: str) -> tuple[bool, str]:
    """Запустить приложение по id из белого списка. Вернуть (успех, текст)."""
    app = _BY_ID.get(app_id)
    if app is None:
        return False, "Приложение не в списке"

    target = app["target"]
    args = app.get("args", [])

    # URL — открываем в браузере по умолчанию.
    if isinstance(target, str) and target.startswith(("http://", "https://")):
        try:
            webbrowser.open(target)
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось открыть URL: %s", target)
            return False, str(exc)

    # Иначе — запускаем как программу.
    try:
        subprocess.Popen([target, *args], creationflags=_NO_WINDOW)
        return True, "ok"
    except FileNotFoundError:
        return False, "Файл не найден (проверь путь в apps_config.py)"
    except Exception as exc:  # noqa: BLE001
        log.exception("Не удалось запустить: %s", target)
        return False, str(exc)
