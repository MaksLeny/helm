"""
Плагин «Приложения»: запуск программ с телефона по белому списку.

Список приложений задаётся в apps_config.py (сервер). Телефон получает список
при открытии раздела и шлёт только id для запуска — путь берётся на сервере.
Команды идут по авторизованному WebSocket (без входа по паролю недоступны).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.plugin_base import Plugin

from . import launcher

log = logging.getLogger("helm")

_SCREEN_HTML: str = (Path(__file__).parent / "screen.html").read_text(encoding="utf-8")

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>'
)


class AppsPlugin(Plugin):
    id = "apps"
    name = "Приложения"
    icon_svg = _ICON

    def frontend(self) -> str:
        return _SCREEN_HTML

    async def handle_ws(self, msg: dict, send) -> None:
        cmd = str(msg.get("cmd", "")).lower()
        loop = asyncio.get_running_loop()
        if cmd in ("sync", "get"):
            apps = await loop.run_in_executor(None, launcher.list_apps)
            await send({"type": "apps_list", "apps": apps})
            return
        if cmd == "launch":
            app_id = str(msg.get("id", ""))
            ok, message = await loop.run_in_executor(None, launcher.launch, app_id)
            log.info("Запуск %s -> %s", app_id, "ok" if ok else message)
            await send({"type": "app_result", "id": app_id, "ok": ok,
                        "action": "launch", "message": message})
            apps = await loop.run_in_executor(None, launcher.list_apps)
            await send({"type": "apps_list", "apps": apps})
            return
        if cmd == "close":
            app_id = str(msg.get("id", ""))
            ok, message = await loop.run_in_executor(None, launcher.close, app_id)
            log.info("Закрытие %s -> %s", app_id, message if ok else "не удалось: " + message)
            await send({"type": "app_result", "id": app_id, "ok": ok,
                        "action": "close", "message": message})
            apps = await loop.run_in_executor(None, launcher.list_apps)
            await send({"type": "apps_list", "apps": apps})
            return
        if cmd == "favorite":
            app_id = str(msg.get("id", ""))
            ok, now_fav = await loop.run_in_executor(None, launcher.toggle_favorite, app_id)
            await send({"type": "app_result", "id": app_id, "ok": ok,
                        "action": "favorite", "favorite": now_fav,
                        "message": "в избранном" if now_fav else "убрано из избранного"})
            apps = await loop.run_in_executor(None, launcher.list_apps)
            await send({"type": "apps_list", "apps": apps})
            return
