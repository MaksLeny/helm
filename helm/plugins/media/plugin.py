"""
Медиаплагин Helm — это WinWave, обёрнутый в контракт плагина.

Вся логика (SMTC, обложки, поиск, лайки, громкость) переехала из проекта
winwave-local без изменений: модули audio_control/yandex_music/smtc_worker/
state/media_config лежат рядом. Здесь — только склейка с ядром Helm:
  - frontend(): отдаёт экран-фрагмент (_screen.html);
  - handle_ws(): команды с пульта -> handle_media_command;
  - start()/stop(): SMTC-слушатель, тикер прогресса, общий http-клиент;
  - router: HTTP-эндпоинты /search, /current_cover.jpg, /history_cover.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Response

from core.plugin_base import Plugin

from .state import media_state, history, SERVER_IP, _history_payload
from .state import _get_http, _close_http
from .smtc_worker import SmtcListener, _progress_ticker
from . import smtc_worker
from .yandex_music import _yandex_search_tracks
from .commands import handle_media_command

log = logging.getLogger("winwave")

_PAGE_HTML: str = (Path(__file__).parent / "_page.html").read_text(encoding="utf-8")

# Иконка плитки в меню Helm (нота).
_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'
)

# Прозрачный 1×1 PNG — отдаём вместо 404, когда обложки нет (плеер закрыт),
# чтобы <img> на фронте не сыпал ошибками в консоль сервера и браузера.
import base64 as _b64
_BLANK_PNG = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class MediaPlugin(Plugin):
    id = "media"
    name = "Мультимедиа"
    icon_svg = _ICON

    def __init__(self) -> None:
        super().__init__()
        self._listener: SmtcListener | None = None
        self._ticker: asyncio.Task | None = None
        self.router = self._build_router()

    # --- Фронтенд -----------------------------------------------------------
    def frontend(self) -> str:
        # Экран WinWave показываем в изолированном iframe: его глобальный CSS,
        # фиксированные слои и собственный скрипт не должны влиять на оболочку
        # Helm. allow=… разрешает гироскоп (параллакс) и полноэкранный режим.
        return (
            '<iframe class="media-frame" src="/plugin/media/page" '
            'title="Мультимедиа" '
            'allow="gyroscope; accelerometer; fullscreen">'
            '</iframe>'
        )

    # --- WebSocket ----------------------------------------------------------
    async def handle_ws(self, msg: dict, send) -> None:
        await handle_media_command(msg, send)

    # --- Жизненный цикл -----------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._listener = SmtcListener(loop)
        await self._listener.start()
        smtc_worker.smtc_listener = self._listener   # для _seek_to
        self._ticker = asyncio.create_task(_progress_ticker())
        _get_http()   # тёплый http-клиент к API Яндекса
        log.info("Медиаплагин запущен (SMTC + тикер)")

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            try:
                await self._ticker
            except asyncio.CancelledError:
                pass
        await _close_http()
        log.info("Медиаплагин остановлен")

    # --- HTTP-роуты ---------------------------------------------------------
    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/plugin/media/page")
        async def media_page():
            from fastapi.responses import HTMLResponse
            return HTMLResponse(content=_PAGE_HTML)

        @router.get("/search")
        async def search(q: str = Query("", description="Поисковый запрос")) -> Response:
            results = await _yandex_search_tracks(q)
            if q.strip() and results:
                history.add_search(q)
                from .state import manager
                await manager.broadcast_json(_history_payload())
            return Response(
                content=json.dumps({"results": results}, ensure_ascii=False),
                media_type="application/json",
            )

        @router.get("/history_cover")
        async def history_cover(k: str = Query(""), v: int = Query(0)) -> Response:
            data = history.cover_bytes(k)
            if data:
                return Response(
                    content=data, media_type="image/jpeg",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "public, max-age=3600",
                    },
                )
            return Response(status_code=404)

        @router.get("/current_cover.jpg")
        async def current_cover() -> Response:
            if media_state.cover_bytes:
                return Response(
                    content=media_state.cover_bytes, media_type="image/jpeg",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                    },
                )
            # Обложки нет (плеер закрыт) — прозрачный пиксель вместо 404,
            # чтобы не засорять логи и не вызывать ошибку загрузки картинки.
            return Response(
                content=_BLANK_PNG, media_type="image/png",
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
            )

        return router


def _detect_ip() -> str:
    return SERVER_IP
