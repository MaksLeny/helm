"""
Плагин «Файлы»: браузер файловой системы ПК с телефона.

WS-команды (msg["plugin"] == "files"):
  • roots            — список корней (диски + домашняя папка)
  • list  {path}     — содержимое папки
  • delete {path}    — удалить файл/папку
  • sync             — то же, что roots (для onShow)

HTTP-роуты (требуют сессию Helm):
  • GET  /plugin/files/download?path=...  — скачать файл на телефон
  • POST /plugin/files/upload (dir + file) — загрузить файл в папку ПК

Безопасность целиком в fs.py: любой путь нормализуется и проверяется на
принадлежность разрешённым корням; системные каталоги скрыты. Блокирующие
операции с ФС уводятся в executor, чтобы не морозить event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from core.auth import sessions
from core.config import SESSION_COOKIE
from core.plugin_base import Plugin

from . import fs

log = logging.getLogger("helm")

_SCREEN_HTML: str = (Path(__file__).parent / "screen.html").read_text(encoding="utf-8")

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 5a2 2 0 0 1 2-2h4l2 3h6a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>'
    '</svg>'
)

# multipart нужен для загрузки файлов; без него браузер всё равно работает
# (навигация, скачивание), только загрузка отключается.
try:
    import multipart as _multipart  # noqa: F401
    _HAS_MULTIPART = True
except Exception:  # noqa: BLE001
    try:
        import python_multipart as _multipart  # noqa: F401
        _HAS_MULTIPART = True
    except Exception:  # noqa: BLE001
        _HAS_MULTIPART = False


def _authed(request: Request) -> bool:
    return sessions.valid(request.cookies.get(SESSION_COOKIE))


class FilesPlugin(Plugin):
    id = "files"
    name = "Файлы"
    icon_svg = _ICON

    def __init__(self) -> None:
        super().__init__()
        self.router = self._build_router()

    def frontend(self) -> str:
        return _SCREEN_HTML

    # --- HTTP ---------------------------------------------------------------
    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/plugin/files")

        @router.get("/download")
        async def download(request: Request, path: str = "") -> Response:
            if not _authed(request):
                return Response(status_code=401)
            loop = asyncio.get_running_loop()
            p = await loop.run_in_executor(None, fs.file_for_download, path)
            if p is None:
                return Response(status_code=404)
            import mimetypes
            mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            return FileResponse(str(p), media_type=mime, filename=p.name)

        if _HAS_MULTIPART:

            @router.post("/upload")
            async def upload(request: Request) -> Response:  # noqa: F811
                if not _authed(request):
                    return JSONResponse({"ok": False, "error": "не авторизован"}, status_code=401)
                try:
                    form = await request.form()
                except Exception as e:  # noqa: BLE001
                    return JSONResponse({"ok": False, "error": f"форма не прочитана: {e}"},
                                        status_code=400)
                dir_path = str(form.get("dir") or "")
                upload_file = None
                for value in form.values():
                    if hasattr(value, "filename") and hasattr(value, "read"):
                        upload_file = value
                        break
                if not dir_path or upload_file is None:
                    return JSONResponse({"ok": False, "error": "нет папки или файла"},
                                        status_code=400)
                data = await upload_file.read()
                filename = os.path.basename(upload_file.filename or "file")
                loop = asyncio.get_running_loop()
                saved = await loop.run_in_executor(None, fs.save_upload, dir_path, filename, data)
                if not saved:
                    return JSONResponse({"ok": False, "error": "не удалось сохранить (путь/права)"},
                                        status_code=400)
                return JSONResponse({"ok": True, "path": saved})
        else:
            log.warning("Файлы: python-multipart не установлен — загрузка в папки "
                        "ПК отключена (навигация и скачивание работают)")

            @router.post("/upload")
            async def upload_disabled(request: Request) -> Response:
                return JSONResponse(
                    {"ok": False, "error": "загрузка отключена: нет python-multipart"},
                    status_code=501)

        return router

    # --- WS -----------------------------------------------------------------
    async def handle_ws(self, msg: dict, send) -> None:
        cmd = str(msg.get("cmd", "")).lower()
        loop = asyncio.get_running_loop()

        if cmd in ("roots", "sync", "get"):
            await send({"type": "files_roots", "roots": fs.list_roots(),
                        "uploads_ok": _HAS_MULTIPART})
            return

        if cmd == "list":
            path = str(msg.get("path", ""))
            data = await loop.run_in_executor(None, fs.list_dir, path)
            if data is None:
                await send({"type": "files_error", "message": "Папка недоступна"})
                return
            await send({"type": "files_list", **data})
            return

        if cmd == "delete":
            path = str(msg.get("path", ""))
            ok, message = await loop.run_in_executor(None, fs.delete_path, path)
            log.info("Файлы: delete %s -> %s", path, message)
            await send({"type": "files_result", "ok": ok, "action": "delete",
                        "message": message, "path": path})
            return

        if cmd == "open":
            path = str(msg.get("path", ""))
            ok, message = await loop.run_in_executor(None, fs.open_on_pc, path)
            log.info("Файлы: open %s -> %s", path, message)
            await send({"type": "files_result", "ok": ok, "action": "open",
                        "message": message, "path": path})
            return
