"""
Helm — реестр плагинов.

На шаге 1 список плагинов пуст: мы строим каркас. Плагины (медиа, монитор…)
будут регистрироваться здесь на следующих шагах. Реестр — единственное место,
которое знает обо всех плагинах; ядро спрашивает у него список для меню и
монтирования роутов.
"""
from __future__ import annotations

import logging

from .plugin_base import Plugin

log = logging.getLogger("helm")

# Порядок в списке = порядок плиток в меню.
_PLUGINS: list[Plugin] = []


def register(plugin: Plugin) -> None:
    """Добавить плагин в реестр."""
    _PLUGINS.append(plugin)
    log.info("Зарегистрирован плагин: %s (%s)", plugin.name, plugin.id)


def all_plugins() -> list[Plugin]:
    """Все зарегистрированные плагины (в порядке регистрации)."""
    return list(_PLUGINS)


def load_plugins() -> None:
    """
    Подключить плагины. Пока (шаг 1) — ничего: каркас без разделов.

    На шаге 2 здесь появится регистрация медиаплагина WinWave, на шаге 3 —
    монитора, и т.д. Держим импорты внутри функции, чтобы плагин с ошибкой
    не валил весь сервер на этапе импорта.
    """
    # --- сюда будут добавляться плагины ---
    try:
        from plugins.media import MediaPlugin
        register(MediaPlugin())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось загрузить медиаплагин")
    try:
        from plugins.monitor import MonitorPlugin
        register(MonitorPlugin())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось загрузить монитор")
    try:
        from plugins.power import PowerPlugin
        register(PowerPlugin())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось загрузить питание")
    try:
        from plugins.apps import AppsPlugin
        register(AppsPlugin())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось загрузить приложения")
    try:
        from plugins.clipboard import ClipboardPlugin
        register(ClipboardPlugin())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось загрузить буфер обмена")
    try:
        from plugins.files import FilesPlugin
        register(FilesPlugin())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось загрузить файлы")
    try:
        from plugins.windows import WindowsPlugin
        register(WindowsPlugin())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось загрузить окна")
    log.info("Плагинов загружено: %d", len(_PLUGINS))
