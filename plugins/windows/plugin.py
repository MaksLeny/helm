"""
Плагин «Окна»: свернуть всё / восстановить / список окон + переключение.

WS-команды (plugin == "windows"):
  • sync / list        — список открытых окон
  • minimize_all       — свернуть все окна (показать рабочий стол)
  • restore_all        — восстановить свёрнутые окна
  • activate {id}      — сделать окно активным

Все блокирующие вызовы уходят в executor. Доступ по ролям обеспечивает ядро.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.plugin_base import Plugin

from . import winmgr

log = logging.getLogger("helm")

_SCREEN_HTML: str = (Path(__file__).parent / "screen.html").read_text(encoding="utf-8")

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M3 9h18"/>'
    '</svg>'
)


class WindowsPlugin(Plugin):
    id = "windows"
    name = "Окна"
    icon_svg = _ICON

    def frontend(self) -> str:
        return _SCREEN_HTML

    async def handle_ws(self, msg: dict, send) -> None:
        cmd = str(msg.get("cmd", "")).lower()
        loop = asyncio.get_running_loop()

        if cmd in ("sync", "list", "get"):
            wins = await loop.run_in_executor(None, winmgr.list_windows)
            await send({"type": "windows_list", "windows": wins,
                        "supported": bool(wins) or winmgr._HAS_WIN32})
            return

        if cmd == "minimize_all":
            ok, message = await loop.run_in_executor(None, winmgr.minimize_all)
            await send({"type": "windows_result", "ok": ok, "action": "minimize_all",
                        "message": message})
            return

        if cmd == "restore_all":
            ok, message = await loop.run_in_executor(None, winmgr.restore_all)
            await send({"type": "windows_result", "ok": ok, "action": "restore_all",
                        "message": message})
            return

        if cmd == "activate":
            wid = str(msg.get("id", ""))
            ok, message = await loop.run_in_executor(None, winmgr.activate_window, wid)
            await send({"type": "windows_result", "ok": ok, "action": "activate",
                        "message": message})
            # обновим список
            wins = await loop.run_in_executor(None, winmgr.list_windows)
            await send({"type": "windows_list", "windows": wins,
                        "supported": bool(wins) or winmgr._HAS_WIN32})
            return
