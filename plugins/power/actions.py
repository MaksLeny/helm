"""
Действия питания на Windows.

Все действия — стандартные системные команды, без сторонних утилит. Запускаем
их через subprocess со скрытым окном, чтобы не мигала консоль. Возвращаем
(ok, message), чтобы плагин мог сообщить результат на телефон.

ВАЖНО (безопасность): этот модуль ничего сам не решает про доступ. Команды сюда
доходят только из обработчика плагина, который вызывается ядром Helm лишь для
WebSocket с валидной сессией. То есть без входа по паролю выполнить их нельзя.
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("helm")

_IS_WIN = sys.platform.startswith("win")
# Флаг, чтобы не мелькало окно консоли (только Windows).
_NO_WINDOW = 0x08000000 if _IS_WIN else 0


def _run(args: list[str]) -> tuple[bool, str]:
    """Запустить команду, вернуть (успех, текст)."""
    try:
        subprocess.Popen(args, creationflags=_NO_WINDOW)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        log.exception("Команда питания не выполнилась: %r", args)
        return False, str(exc)


# --- Безопасные/обратимые ---------------------------------------------------

def lock() -> tuple[bool, str]:
    """Заблокировать экран (как Win+L)."""
    return _run(["rundll32.exe", "user32.dll,LockWorkStation"])


def sleep() -> tuple[bool, str]:
    """Усыпить ПК. Важно: hibernate должен быть выключен, иначе уйдёт в гибернацию."""
    return _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])


def hibernate() -> tuple[bool, str]:
    """Гибернация (сохранить в файл и выключить питание)."""
    return _run(["shutdown", "/h"])


# --- Необратимые (требуют подтверждения на клиенте) -------------------------

def shutdown() -> tuple[bool, str]:
    """Выключить ПК немедленно."""
    return _run(["shutdown", "/s", "/t", "0"])


def restart() -> tuple[bool, str]:
    """Перезагрузить ПК немедленно."""
    return _run(["shutdown", "/r", "/t", "0"])


def logoff() -> tuple[bool, str]:
    """Выйти из системы (завершить сеанс пользователя)."""
    return _run(["shutdown", "/l"])


def cancel() -> tuple[bool, str]:
    """Отменить запланированное выключение/перезагрузку (страховка)."""
    return _run(["shutdown", "/a"])


# Карта команд: id -> функция. Помечаем, какие необратимые.
ACTIONS = {
    "lock": lock,
    "sleep": sleep,
    "hibernate": hibernate,
    "shutdown": shutdown,
    "restart": restart,
    "logoff": logoff,
    "cancel": cancel,
}
