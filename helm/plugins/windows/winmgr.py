"""
Helm · окна — управление окнами Windows с телефона.

Возможности:
  • свернуть все окна (показать рабочий стол);
  • восстановить окна;
  • список видимых окон верхнего уровня + переключение (активация).

Список и активация окон требуют win32gui (pywin32). Если его нет — список
будет пустым, но «свернуть всё» работает через Shell.MinimizeAll (COM) и
запасным способом через PowerShell. Linux: всё это недоступно (заглушки).
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("helm")

_IS_WIN = sys.platform.startswith("win")

try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
    _HAS_WIN32 = True
except Exception:  # noqa: BLE001
    _HAS_WIN32 = False


def _shell_minimize_all() -> tuple[bool, str]:
    """Свернуть все окна через Shell COM (как Win+D)."""
    if not _IS_WIN:
        return False, "только для Windows"
    try:
        import comtypes.client  # type: ignore
        shell = comtypes.client.CreateObject("Shell.Application")
        shell.MinimizeAll()
        return True, "свернуто"
    except Exception as e:  # noqa: BLE001
        # запасной путь — PowerShell со Shell.Application
        try:
            import subprocess
            ps = ("(New-Object -ComObject Shell.Application).MinimizeAll()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=8,
                           creationflags=0x08000000)
            return True, "свернуто"
        except Exception as e2:  # noqa: BLE001
            log.warning("Окна: не удалось свернуть всё: %s / %s", e, e2)
            return False, "не удалось свернуть"


def _shell_restore_all() -> tuple[bool, str]:
    """Восстановить свёрнутые окна (отмена «свернуть всё»)."""
    if not _IS_WIN:
        return False, "только для Windows"
    try:
        import comtypes.client  # type: ignore
        shell = comtypes.client.CreateObject("Shell.Application")
        shell.UndoMinimizeALL()
        return True, "восстановлено"
    except Exception as e:  # noqa: BLE001
        # запасной путь — PowerShell со Shell.Application
        try:
            import subprocess
            ps = "(New-Object -ComObject Shell.Application).UndoMinimizeALL()"
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=8,
                               creationflags=0x08000000)
            if r.returncode == 0:
                return True, "восстановлено"
            return False, "не удалось восстановить"
        except Exception as e2:  # noqa: BLE001
            log.warning("Окна: не удалось восстановить: %s / %s", e, e2)
            return False, "не удалось восстановить"


def minimize_all() -> tuple[bool, str]:
    return _shell_minimize_all()


def restore_all() -> tuple[bool, str]:
    return _shell_restore_all()


def list_windows() -> list[dict]:
    """Список видимых окон верхнего уровня: [{id, title}]. Пустой без win32."""
    if not (_IS_WIN and _HAS_WIN32):
        return []
    out: list[dict] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title or not title.strip():
                return
            # пропускаем служебные/нулевые окна
            out.append({"id": str(hwnd), "title": title})
        except Exception:  # noqa: BLE001
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as e:  # noqa: BLE001
        log.debug("EnumWindows error: %s", e)
    # ограничим разумным числом, самые свежие сверху не гарантировано —
    # просто отдаём список, фронт отсортирует по названию
    return out[:40]


def activate_window(hwnd_str: str) -> tuple[bool, str]:
    """Сделать окно активным (развернуть, если свёрнуто, и поднять)."""
    if not (_IS_WIN and _HAS_WIN32):
        return False, "недоступно (нет win32)"
    try:
        hwnd = int(hwnd_str)
    except (TypeError, ValueError):
        return False, "неверный id окна"
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True, "переключено"
    except Exception as e:  # noqa: BLE001
        log.warning("Окна: не удалось активировать %s: %s", hwnd_str, e)
        return False, "не удалось переключить"
