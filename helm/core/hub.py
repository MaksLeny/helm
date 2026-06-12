"""
Helm — WebSocket-хаб.

Держит активные соединения с телефонами и маршрутизирует сообщения:
  - входящие: по полю msg["plugin"] отдаём нужному плагину;
  - исходящие: плагин может разослать сообщение всем (broadcast), ядро
    проставит ему правильный адрес plugin=<id>.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket

if TYPE_CHECKING:
    from .plugin_base import Plugin

log = logging.getLogger("helm")


class WebSocketHub:
    def __init__(self) -> None:
        self.connections: list[WebSocket] = []
        # id плагина -> экземпляр плагина (заполняет реестр при регистрации)
        self.plugins: dict[str, "Plugin"] = {}
        # Проверка доступа: (ws, plugin_id) -> bool. Ставит main при старте.
        # По умолчанию всё разрешено (если контроль доступа не настроен).
        self.access_check = None

    def register_plugin(self, plugin: "Plugin") -> None:
        self.plugins[plugin.id] = plugin

    # --- Соединения ---------------------------------------------------------
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.append(ws)
        log.info("Телефон подключился (всего: %d)", len(self.connections))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)
        log.info("Телефон отключился (всего: %d)", len(self.connections))

    # --- Рассылка -----------------------------------------------------------
    async def broadcast(self, plugin_id: str, payload: dict) -> None:
        """Разослать сообщение всем подключённым от имени плагина."""
        payload = {**payload, "plugin": plugin_id}
        text = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        # Итерируем по копии: во время await send_text список соединений может
        # измениться (другой клиент отключился) — без копии будет ошибка
        # "list changed during iteration".
        for ws in list(self.connections):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    # --- Маршрутизация входящих --------------------------------------------
    async def route_inbound(self, ws: WebSocket, raw: str) -> None:
        """Разобрать входящее сообщение и отдать его адресованному плагину."""
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            log.debug("Не-JSON сообщение по WS, игнор: %r", raw[:80])
            return

        # Системный ping/pong heartbeat ядра — без плагина
        if msg.get("type") == "pong":
            return

        plugin_id = msg.get("plugin")
        plugin = self.plugins.get(plugin_id) if plugin_id else None
        if plugin is None:
            log.debug("Сообщение без известного плагина: %r", plugin_id)
            return

        # Контроль доступа: гость не может слать команды чужим плагинам.
        if self.access_check is not None:
            try:
                if not self.access_check(ws, plugin_id):
                    log.warning("Доступ к плагину %s запрещён для этой сессии", plugin_id)
                    return
            except Exception:  # noqa: BLE001
                return

        async def send(payload: dict) -> None:
            payload = {**payload, "plugin": plugin_id}
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass

        try:
            await plugin.handle_ws(msg, send)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в обработчике плагина %s", plugin_id)


hub = WebSocketHub()
