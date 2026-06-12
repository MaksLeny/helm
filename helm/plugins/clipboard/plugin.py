"""
Плагин «Буфер обмена»: двусторонний обмен текстом, ссылками, картинками и
файлами телефон ↔ ПК, с историей и авто-слежением за буфером ПК.

Команды по авторизованному WebSocket (msg["plugin"] == "clipboard"):
  • sync             — отдать текущую историю + состояние авто-слежения
  • push  {text}     — телефон → ПК: положить текст в системный буфер ПК
  • pull             — ПК → телефон: прочитать буфер ПК (текст/картинка/файлы)
  • copy  {id}       — повторно положить текст из истории в буфер ПК
  • copy_image {id}  — положить картинку из истории в буфер ПК
  • open  {id|text}  — открыть ссылку из записи в браузере ПК
  • delete {id}      — удалить запись из истории
  • clear            — очистить историю
  • watch  {on}      — включить/выключить авто-слежение за буфером ПК

HTTP-роуты (требуют сессию Helm):
  • POST /plugin/clipboard/upload         — телефон загружает файл/картинку на ПК
  • GET  /plugin/clipboard/blob/{blob_id} — скачать/просмотреть переданный файл

Картинки/файлы хранятся во временной папке (blobs.py), история — метаданные
(store.py). Любое изменение истории рассылается всем телефонам (broadcast).
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from core.auth import sessions
from core.config import SESSION_COOKIE
from core.hub import hub
from core.plugin_base import Plugin

from . import blobs, clip, opener
from .store import ClipStore

log = logging.getLogger("helm")

_SCREEN_HTML: str = (Path(__file__).parent / "screen.html").read_text(encoding="utf-8")

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="8" y="2" width="8" height="4" rx="1"/>'
    '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>'
    '<path d="M9 12h6M9 16h4"/></svg>'
)

_WATCH_PERIOD = 1.5  # как часто опрашивать буфер ПК при авто-слежении (сек)
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Загрузка файлов (UploadFile) требует python-multipart. Если его нет — плагин
# всё равно работает (текст, история, авто-слежение, скачивание blob'ов),
# просто отключаем роут загрузки. НЕЛЬЗЯ позволить отсутствию опц. зависимости
# уронить весь плагин (иначе плитка пропадёт из меню).
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


class ClipboardPlugin(Plugin):
    id = "clipboard"
    name = "Буфер обмена"
    icon_svg = _ICON

    def __init__(self) -> None:
        super().__init__()
        self.store = ClipStore()
        self._available = clip.clipboard_available()
        if not self._available:
            log.warning("Буфер обмена ОС недоступен — обмен с ПК будет ограничен")
        # Постоянная память: подгружаем историю с диска (переживает перезагрузку).
        self.store.load()
        self._watch = False
        self._watch_task: asyncio.Task | None = None
        self._last_seen_text: str | None = None
        self._last_seen_image: str | None = None
        self.router = self._build_router()

    def frontend(self) -> str:
        return _SCREEN_HTML

    # --- HTTP роуты ---------------------------------------------------------
    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/plugin/clipboard")

        if _HAS_MULTIPART:

            @router.post("/upload")
            async def upload(request: Request) -> Response:  # noqa: F811
                if not _authed(request):
                    return JSONResponse({"ok": False, "error": "не авторизован"}, status_code=401)
                # Читаем форму сами (без строгого File(...) — иначе мобильные
                # браузеры с нестандартной формой ловят 422). Берём первый файл
                # под любым именем поля.
                try:
                    form = await request.form()
                except Exception as e:  # noqa: BLE001
                    return JSONResponse({"ok": False, "error": f"форма не прочитана: {e}"},
                                        status_code=400)
                upload_file = None
                for value in form.values():
                    if hasattr(value, "filename") and hasattr(value, "read"):
                        upload_file = value
                        break
                if upload_file is None:
                    return JSONResponse({"ok": False, "error": "файл не найден в запросе"},
                                        status_code=400)
                data = await upload_file.read()
                if not data:
                    return JSONResponse({"ok": False, "error": "пустой файл"}, status_code=400)
                filename = os.path.basename(upload_file.filename or "file")
                ext = os.path.splitext(filename)[1]
                blob_id = blobs.save_blob(data, ext=ext)
                if not blob_id:
                    return JSONResponse({"ok": False, "error": "слишком большой файл (>50МБ)"},
                                        status_code=413)
                is_image = ext.lower() in _IMG_EXT
                mime = (getattr(upload_file, "content_type", None)
                        or mimetypes.guess_type(filename)[0] or "application/octet-stream")
                # Если расширения нет, но MIME говорит про картинку — считаем картинкой.
                if not is_image and mime.startswith("image/"):
                    is_image = True
                put_to_clip = False
                if is_image:
                    try:
                        loop = asyncio.get_running_loop()
                        ok, err = await loop.run_in_executor(None, clip.set_clipboard_image, data)
                        put_to_clip = ok
                        if not ok:
                            log.warning("Буфер: картинка не легла в буфер ПК: %s", err)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Буфер: ошибка записи картинки в буфер ПК: %s", e)
                        put_to_clip = False
                item = self.store.add_blob(
                    kind="image" if is_image else "file",
                    blob_id=blob_id, filename=filename, size=len(data),
                    mime=mime, src="phone",
                )
                await self._broadcast_history()
                return JSONResponse({"ok": True, "id": item["id"], "blob_id": blob_id,
                                     "to_clipboard": put_to_clip})
        else:
            log.warning("python-multipart не установлен — загрузка файлов в "
                        "буфере отключена (текст/история работают). "
                        "Установите: pip install python-multipart")

            @router.post("/upload")
            async def upload_disabled(request: Request) -> Response:
                return JSONResponse(
                    {"ok": False, "error": "загрузка файлов отключена: на ПК нет "
                     "python-multipart (pip install python-multipart)"},
                    status_code=501)

        @router.get("/blob/{blob_id}")
        async def blob(request: Request, blob_id: str) -> Response:
            if not _authed(request):
                return Response(status_code=401)
            p = blobs.blob_path(blob_id)
            if not p:
                return Response(status_code=404)
            mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            return FileResponse(str(p), media_type=mime, filename=p.name)

        return router

    # --- авто-слежение ------------------------------------------------------
    async def _watch_loop(self) -> None:
        log.info("Буфер: авто-слежение включено")
        loop = asyncio.get_running_loop()
        try:
            while self._watch:
                try:
                    # clip.* запускают PowerShell (блокирующе) — уводим в поток,
                    # чтобы не морозить event loop на сотни мс каждый тик.
                    kind = await loop.run_in_executor(None, clip.clipboard_kind)
                    if kind == "text":
                        ok, text = await loop.run_in_executor(None, clip.get_clipboard)
                        if ok and text and text.strip() and text != self._last_seen_text:
                            self._last_seen_text = text
                            self.store.add(text, src="pc")
                            await self._broadcast_history()
                    elif kind == "image":
                        # Картинку подхватываем тоже: сравниваем по хешу, чтобы
                        # не добавлять одну и ту же повторно каждый тик.
                        ok, payload = await loop.run_in_executor(None, clip.get_clipboard_image)
                        if ok and isinstance(payload, bytes) and payload:
                            import hashlib
                            h = hashlib.md5(payload).hexdigest()
                            if h != self._last_seen_image:
                                self._last_seen_image = h
                                blob_id = blobs.save_blob(payload, ext=".png")
                                if blob_id:
                                    self.store.add_blob(
                                        kind="image", blob_id=blob_id,
                                        filename="Изображение.png", size=len(payload),
                                        mime="image/png", src="pc")
                                    await self._broadcast_history()
                except Exception as e:  # noqa: BLE001
                    log.debug("watch tick error: %s", e)
                await asyncio.sleep(_WATCH_PERIOD)
        finally:
            log.info("Буфер: авто-слежение выключено")

    def _start_watch(self) -> None:
        if self._watch_task and not self._watch_task.done():
            return
        self._watch = True
        self._last_seen_text = None
        self._last_seen_image = None
        self._watch_task = asyncio.create_task(self._watch_loop())

    def _stop_watch(self) -> None:
        self._watch = False
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None

    async def stop(self) -> None:
        self._stop_watch()

    # --- helpers ------------------------------------------------------------
    def _history_payload(self) -> dict:
        return {
            "type": "clip_history",
            "items": self.store.list(),
            "clipboard_ok": self._available,
            "watch": self._watch,
            "uploads_ok": _HAS_MULTIPART,
        }

    async def _broadcast_history(self) -> None:
        await hub.broadcast(self.id, self._history_payload())

    # --- WS -----------------------------------------------------------------
    async def handle_ws(self, msg: dict, send) -> None:
        cmd = str(msg.get("cmd", "")).lower()

        if cmd in ("sync", "get"):
            await send(self._history_payload())
            return

        if cmd == "push":
            text = str(msg.get("text", ""))
            if not text.strip():
                await send({"type": "clip_result", "ok": False,
                            "action": "push", "message": "пустой текст"})
                return
            loop = asyncio.get_running_loop()
            ok, err = await loop.run_in_executor(None, clip.set_clipboard, text)
            self.store.add(text, src="phone")
            self._last_seen_text = text
            await send({"type": "clip_result", "ok": ok, "action": "push",
                        "message": "" if ok else err})
            await self._broadcast_history()
            return

        if cmd == "pull":
            await self._handle_pull(send)
            return

        if cmd == "copy":
            item = self.store.get(str(msg.get("id", "")))
            if not item:
                await send({"type": "clip_result", "ok": False,
                            "action": "copy", "message": "запись не найдена"})
                return
            loop = asyncio.get_running_loop()
            ok, err = await loop.run_in_executor(None, clip.set_clipboard, item.get("text", ""))
            self._last_seen_text = item.get("text", "")
            await send({"type": "clip_result", "ok": ok, "action": "copy",
                        "message": "" if ok else err})
            return

        if cmd == "copy_image":
            item = self.store.get(str(msg.get("id", "")))
            if not item or item.get("kind") != "image" or not item.get("blob_id"):
                await send({"type": "clip_result", "ok": False,
                            "action": "copy_image", "message": "картинка не найдена"})
                return
            p = blobs.blob_path(item["blob_id"])
            if not p:
                await send({"type": "clip_result", "ok": False,
                            "action": "copy_image", "message": "файл недоступен"})
                return
            loop = asyncio.get_running_loop()
            ok, err = await loop.run_in_executor(None, clip.set_clipboard_image, p.read_bytes())
            await send({"type": "clip_result", "ok": ok, "action": "copy_image",
                        "message": "" if ok else err})
            return

        if cmd == "open":
            item = self.store.get(str(msg.get("id", "")))
            text = item["text"] if item else str(msg.get("text", ""))
            ok, err = opener.open_url(text)
            await send({"type": "clip_result", "ok": ok, "action": "open",
                        "message": "" if ok else err})
            return

        if cmd == "delete":
            self.store.delete(str(msg.get("id", "")))
            await send({"type": "clip_result", "ok": True, "action": "delete"})
            await self._broadcast_history()
            return

        if cmd == "clear":
            self.store.clear()
            await send({"type": "clip_result", "ok": True, "action": "clear"})
            await self._broadcast_history()
            return

        if cmd == "watch":
            on = bool(msg.get("on", not self._watch))
            if on:
                self._start_watch()
            else:
                self._stop_watch()
            await send({"type": "clip_result", "ok": True, "action": "watch",
                        "message": "вкл" if on else "выкл"})
            await self._broadcast_history()
            return

    async def _handle_pull(self, send) -> None:
        """Умный pull: определяем, что в буфере ПК — картинка, файлы или текст."""
        loop = asyncio.get_running_loop()
        kind = await loop.run_in_executor(None, clip.clipboard_kind)
        log.info("Буфер: pull, в буфере ПК — %s", kind)

        if kind == "empty":
            await send({"type": "clip_result", "ok": False, "action": "pull",
                        "message": "буфер ПК пуст"})
            return

        if kind == "image":
            ok, payload = await loop.run_in_executor(None, clip.get_clipboard_image)
            if not ok:
                await send({"type": "clip_result", "ok": False, "action": "pull",
                            "message": payload if isinstance(payload, str) else "нет картинки"})
                return
            blob_id = blobs.save_blob(payload, ext=".png")
            if not blob_id:
                await send({"type": "clip_result", "ok": False, "action": "pull",
                            "message": "не удалось сохранить картинку"})
                return
            item = self.store.add_blob(kind="image", blob_id=blob_id,
                                       filename="Изображение.png", size=len(payload),
                                       mime="image/png", src="pc")
            await send({"type": "clip_pulled_blob", "ok": True, "id": item["id"],
                        "blob_id": blob_id, "kind": "image",
                        "filename": item["filename"]})
            await self._broadcast_history()
            return

        if kind == "files":
            ok, paths = await loop.run_in_executor(None, clip.get_clipboard_files)
            if not ok or not isinstance(paths, list):
                await send({"type": "clip_result", "ok": False, "action": "pull",
                            "message": "нет файлов в буфере"})
                return
            added = await loop.run_in_executor(None, self._ingest_files, paths)
            await send({"type": "clip_result", "ok": added > 0, "action": "pull",
                        "message": f"файлов получено: {added}" if added else "не удалось прочитать файлы"})
            await self._broadcast_history()
            return

        ok, payload = await loop.run_in_executor(None, clip.get_clipboard)
        if not ok:
            await send({"type": "clip_result", "ok": False, "action": "pull",
                        "message": payload})
            return
        item = self.store.add(payload, src="pc")
        self._last_seen_text = payload
        await send({"type": "clip_pulled", "ok": True, "text": payload,
                    "id": item["id"] if item else None})
        await self._broadcast_history()

    def _ingest_files(self, paths: list[str]) -> int:
        """Прочитать файлы с диска ПК в blob-хранилище (синхронно, для потока)."""
        added = 0
        for path in paths:
            try:
                pp = Path(path)
                if not pp.is_file():
                    continue
                data = pp.read_bytes()
                if len(data) > 50 * 1024 * 1024:
                    continue
                blob_id = blobs.save_blob(data, ext=pp.suffix)
                if not blob_id:
                    continue
                is_img = pp.suffix.lower() in _IMG_EXT
                self.store.add_blob(
                    kind="image" if is_img else "file",
                    blob_id=blob_id, filename=pp.name, size=len(data),
                    mime=mimetypes.guess_type(pp.name)[0] or "application/octet-stream",
                    src="pc",
                )
                added += 1
            except Exception as e:  # noqa: BLE001
                log.debug("pull file error %s: %s", path, e)
        return added
