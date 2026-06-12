"""
Плагин «Питание»: сон, блокировка, гибернация, выключение, перезагрузка,
выход из системы. Необратимые действия подтверждаются на стороне клиента
(тап «Подтвердить»). Команды доходят сюда только по WebSocket с валидной
сессией (ядро Helm отклоняет неавторизованные соединения).
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.plugin_base import Plugin

from . import actions

log = logging.getLogger("helm")

_SCREEN_HTML: str = (Path(__file__).parent / "screen.html").read_text(encoding="utf-8")

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg>'
)


class PowerPlugin(Plugin):
    id = "power"
    name = "Питание"
    icon_svg = _ICON

    def frontend(self) -> str:
        return _SCREEN_HTML

    async def handle_ws(self, msg: dict, send) -> None:
        cmd = str(msg.get("cmd", "")).lower()
        fn = actions.ACTIONS.get(cmd)
        if fn is None:
            return
        ok, message = fn()
        log.info("Питание: %s -> %s", cmd, "ok" if ok else message)
        # Сообщаем клиенту результат (особенно если не получилось).
        await send({"type": "power_result", "cmd": cmd, "ok": ok, "message": message})
