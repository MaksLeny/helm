"""
Обработка команд медиаплагина, приходящих по WebSocket через хаб Helm.

Раньше команды приходили «голой» строкой и парсились в _handle_command(raw).
Теперь ядро Helm отдаёт уже разобранный dict (с полем plugin == "media"),
поэтому работаем со словарём. Логика действий не изменилась.
"""
from __future__ import annotations

import logging

from .media_config import (
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PREV_TRACK,
)
from .state import (
    media_state,
    manager,
    SERVER_IP,
    _history_payload,
    _apps_payload,
)
from .audio_control import (
    _press_media_key,
    volume_controller,
    app_volume_controller,
)
from .yandex_music import (
    _yandex_like_current_track,
    _open_yandex_track,
    _yandex_add_track,
)
from .smtc_worker import _seek_to

log = logging.getLogger("winwave")


async def handle_media_command(msg: dict, send) -> None:
    """
    Выполнить команду медиаплагина.

    msg — словарь от хаба Helm. Поддерживаются:
      - простые действия:  {"cmd": "play"|"pause"|"next"|"previous"|"like"|"mute"}
      - сервисные:         {"cmd": "sync"|"get_apps"}
      - с параметром:      {"cmd": "volume"|"seek"|"app_volume"|"app_mute"|
                                   "play_track"|"add_track", ...}
    send(payload) — отправить ответ конкретному клиенту (ядро проставит plugin).
    """
    cmd = str(msg.get("cmd", "")).lower()
    value = msg.get("value")

    if not cmd:
        return

    # --- Сервисные ---------------------------------------------------------
    if cmd == "sync":
        # Клиент открыл раздел/переподключился — шлём ему текущее состояние.
        await send(media_state.as_payload(SERVER_IP))
        await send(_history_payload())
        return
    if cmd == "get_apps":
        await send(_apps_payload())
        return

    # --- С параметром ------------------------------------------------------
    if cmd == "volume" and value is not None:
        volume_controller.set_level(int(value))
        return
    if cmd == "seek" and value is not None:
        await _seek_to(float(value))
        return
    if cmd == "app_volume" and value is not None:
        app_volume_controller.set_app_volume(str(msg.get("key", "")), int(value))
        return
    if cmd == "app_mute":
        app_volume_controller.toggle_app_mute(str(msg.get("key", "")))
        await manager.broadcast_json(_apps_payload())
        return
    if cmd == "play_track":
        _open_yandex_track(str(msg.get("trackId", "")), str(msg.get("albumId", "")))
        return
    if cmd == "add_track":
        await _yandex_add_track(
            str(msg.get("trackId", "")), str(msg.get("albumId", ""))
        )
        return

    # --- Простые действия --------------------------------------------------
    log.info("Команда: %s", cmd)
    if cmd in ("play", "pause"):
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
    elif cmd == "next":
        _press_media_key(VK_MEDIA_NEXT_TRACK)
    elif cmd == "previous":
        _press_media_key(VK_MEDIA_PREV_TRACK)
    elif cmd == "like":
        await _yandex_like_current_track()
    elif cmd == "mute":
        volume_controller.toggle_mute()
        await manager.broadcast_json(media_state.as_payload(SERVER_IP))
    else:
        log.warning("Неизвестная команда media: %s", cmd)
