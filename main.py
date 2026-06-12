"""
Helm — точка входа.

Ядро: поднимает FastAPI/uvicorn, обслуживает вход по паролю, отдаёт оболочку
дашборда, монтирует роуты плагинов, держит единый WebSocket с heartbeat и не
даёт ПК уснуть, пока сервер работает.

Запуск:
    pip install -r requirements.txt
    python main.py
Затем на телефоне:  http://<IP-ПК>:8765
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import socket
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import (
    HOST, PORT, WEB_DIR, SESSION_COOKIE, SESSION_TTL,
)
from core.auth import verify_password, sessions, hash_password, set_password_hash
from core import settings as app_settings
from core import events as app_events
from core.hub import hub
from core import registry

log = logging.getLogger("helm")

# Статика ядра читается один раз при старте.
LOGIN_HTML: str = (WEB_DIR / "login.html").read_text(encoding="utf-8")
SHELL_HTML: str = (WEB_DIR / "shell.html").read_text(encoding="utf-8")
MANIFEST_JSON: str = (WEB_DIR / "manifest.json").read_text(encoding="utf-8")

HEARTBEAT_INTERVAL: float = 10.0


# --------------------------------------------------------------------------- #
#                          ОПРЕДЕЛЕНИЕ IP ДЛЯ ПОДСКАЗКИ                        #
# --------------------------------------------------------------------------- #
def _detect_server_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        sock.close()


SERVER_IP: str = _detect_server_ip()


# --------------------------------------------------------------------------- #
#         АНТИ-СОН: не даём Windows усыпить ПК, пока сервер работает          #
# --------------------------------------------------------------------------- #
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _keep_awake_on() -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        log.info("Анти-сон включён: ПК не уснёт, пока Helm работает")
    except Exception:  # noqa: BLE001
        log.warning("Анти-сон недоступен (не Windows?)")


def _keep_awake_off() -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
#                          СБОРКА ОБОЛОЧКИ ДАШБОРДА                            #
# --------------------------------------------------------------------------- #
def _render_shell() -> str:
    """Подставить в оболочку плитки меню и экраны зарегистрированных плагинов."""
    plugins = registry.all_plugins()
    if plugins:
        tiles = "\n".join(_tile_html(p) for p in plugins)
        screens = "\n".join(
            f'<div class="plugin-screen" id="plugin-{p.id}">{p.frontend()}</div>'
            for p in plugins
        )
    else:
        tiles = (
            '<div class="empty">Пока нет разделов.<br>'
            'Плагины появятся на следующих шагах сборки.</div>'
        )
        screens = ""
    return SHELL_HTML.replace("{{TILES}}", tiles).replace("{{SCREENS}}", screens)


def _tile_html(p) -> str:
    """HTML одной плитки меню для плагина."""
    icon = p.icon_svg or (
        '<svg viewBox="0 0 24 24" fill="currentColor">'
        '<circle cx="12" cy="12" r="5"/></svg>'
    )
    # Экранируем имя/id — на случай спецсимволов, чтобы не ломать разметку.
    from html import escape as _esc
    pid = _esc(str(p.id), quote=True)
    name = _esc(str(p.name), quote=True)
    return (
        f'<button class="tile" data-plugin="{pid}" data-name="{name}">'
        f'<span class="ic">{icon}</span>'
        f'<span class="label">{name}</span>'
        f'</button>'
    )


# --------------------------------------------------------------------------- #
#                              ЖИЗНЕННЫЙ ЦИКЛ                                  #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_app: FastAPI):
    _keep_awake_on()
    # Контроль доступа по ролям: гость видит только разрешённые разделы.
    hub.access_check = lambda ws, plugin_id: _can_access(
        ws.cookies.get(SESSION_COOKIE), plugin_id)
    # Плагины уже загружены и смонтированы на уровне модуля (ниже). Здесь
    # только поднимаем их фоновые задачи (слушатели, тикеры).
    for p in registry.all_plugins():
        try:
            await p.start()
        except Exception:  # noqa: BLE001
            log.exception("Плагин %s не смог стартовать", p.id)
    log.info("=" * 56)
    log.info("Helm запущен!")
    log.info("Разделов: %d", len(registry.all_plugins()))
    log.info("Открой на телефоне:  http://%s:%d", SERVER_IP, PORT)
    log.info("=" * 56)
    yield
    for p in registry.all_plugins():
        try:
            await p.stop()
        except Exception:  # noqa: BLE001
            pass
    _keep_awake_off()
    log.info("Helm остановлен")


app = FastAPI(title="Helm", lifespan=lifespan)

# Плагины загружаем и монтируем СРАЗУ (роуты должны быть зарегистрированы до
# старта сервера). Фоновые задачи плагинов поднимаются в lifespan.start().
registry.load_plugins()
for _p in registry.all_plugins():
    hub.register_plugin(_p)
    if getattr(_p, "router", None) is not None:
        app.include_router(_p.router)


@app.middleware("http")
async def _plugin_access_guard(request: Request, call_next):
    """Запрещает гостю обращаться к HTTP-роутам недоступных ему плагинов.

    Покрывает прямые URL вроде /plugin/files/download — чтобы гость не мог
    скачать файлы или дёрнуть чужой плагин в обход скрытого меню.
    """
    path = request.url.path
    if path.startswith("/plugin/"):
        parts = path.split("/", 3)   # ['', 'plugin', '<id>', '...']
        if len(parts) >= 3 and parts[2]:
            plugin_id = parts[2]
            token = request.cookies.get(SESSION_COOKIE)
            if not _can_access(token, plugin_id):
                return JSONResponse({"error": "доступ запрещён"}, status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------- #
#                           АУТЕНТИФИКАЦИЯ (РОУТЫ)                             #
# --------------------------------------------------------------------------- #
def _authed(request: Request) -> bool:
    return sessions.valid(request.cookies.get(SESSION_COOKIE))


def _https_enabled() -> bool:
    """Включён ли HTTPS в настройках (env > файл)."""
    val = app_settings.get("https", False)
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return bool(val)


def _guest_allowed() -> list[str]:
    """Разделы, доступные гостю (пересечение настройки с реально загруженными)."""
    loaded = {p.id for p in registry.all_plugins()}
    return [pid for pid in app_settings.get_guest_plugins() if pid in loaded]


def _can_access(request_or_token, plugin_id: str) -> bool:
    """Может ли владелец сессии обращаться к данному плагину."""
    token = request_or_token
    if hasattr(request_or_token, "cookies"):
        token = request_or_token.cookies.get(SESSION_COOKIE)
    if sessions.is_admin(token):
        return True
    if not sessions.valid(token):
        return False
    return plugin_id in _guest_allowed()


@app.post("/auth/login")
async def auth_login(request: Request) -> Response:
    """Проверить АДМИН-пароль, повысить до админ-сессии."""
    token0 = request.cookies.get(SESSION_COOKIE)
    who = sessions.name(token0)
    try:
        data = await request.json()
        password = str(data.get("password", ""))
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False}, status_code=400)

    if not verify_password(password):
        app_events.add_event(
            "admin_fail",
            f"Неудачная попытка входа в админку{(' — ' + who) if who else ''}",
            who=who)
        return JSONResponse({"ok": False}, status_code=401)

    token = sessions.create(role="admin", name=who)
    app_events.add_event("admin_ok", "Вход в админку выполнен", who=who)
    resp = JSONResponse({"ok": True, "role": "admin"})
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_TTL), httponly=True, samesite="lax",
    )
    return resp


@app.get("/auth/role")
async def auth_role(request: Request) -> Response:
    """Текущая роль сессии + список разрешённых разделов + имя (для фронта)."""
    token = request.cookies.get(SESSION_COOKIE)
    role = sessions.role(token) or "guest"
    if role == "admin":
        allowed = [p.id for p in registry.all_plugins()]
    else:
        allowed = _guest_allowed()
    return JSONResponse({"role": role, "allowed": allowed,
                         "name": sessions.name(token)})


@app.post("/auth/name")
async def auth_name(request: Request) -> Response:
    """Гость представляется: сохраняем имя, пишем событие и шлём тост на ПК."""
    token = request.cookies.get(SESSION_COOKIE)
    if not sessions.valid(token):
        return JSONResponse({"ok": False, "error": "нет сессии"}, status_code=401)
    try:
        data = await request.json()
        name = str(data.get("name", "")).strip()[:40]
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False}, status_code=400)
    if not name:
        return JSONResponse({"ok": False, "error": "пустое имя"}, status_code=400)
    was = sessions.name(token)
    sessions.set_name(token, name)
    # Событие и тост только при первом представлении (или смене имени).
    if name != was:
        role = sessions.role(token) or "guest"
        if role != "admin":
            app_events.add_event("guest_join", f"Гость зашёл: {name}", who=name)
            app_events.toast_pc("Helm — гость", f"{name} подключился")
    return JSONResponse({"ok": True, "name": name})


@app.get("/admin/online")
async def admin_online(request: Request) -> Response:
    """Кто сейчас онлайн (только админ)."""
    if not sessions.is_admin(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"error": "нужен админ-доступ"}, status_code=403)
    return JSONResponse({"online": sessions.online(within_seconds=40)})


@app.get("/admin/events")
async def admin_events(request: Request) -> Response:
    """Лог действий (только админ)."""
    if not sessions.is_admin(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"error": "нужен админ-доступ"}, status_code=403)
    return JSONResponse({"events": app_events.recent(120)})


@app.post("/admin/events/clear")
async def admin_events_clear(request: Request) -> Response:
    if not sessions.is_admin(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"error": "нужен админ-доступ"}, status_code=403)
    app_events.clear()
    return JSONResponse({"ok": True})


@app.post("/auth/logout")
async def auth_logout(request: Request) -> Response:
    """Выход из админ-режима → возврат к гостю (revoke + новая гостевая)."""
    sessions.revoke(request.cookies.get(SESSION_COOKIE))
    token = sessions.create(role="guest")
    resp = JSONResponse({"ok": True, "role": "guest"})
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_TTL), httponly=True, samesite="lax",
    )
    return resp


# --------------------------------------------------------------------------- #
#                              НАСТРОЙКИ СЕРВЕРА                               #
# --------------------------------------------------------------------------- #
@app.get("/settings")
async def settings_get(request: Request) -> Response:
    """Текущие настройки сервера (только админ)."""
    if not sessions.is_admin(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"error": "нужен админ-доступ"}, status_code=403)
    return JSONResponse({
        "port": PORT,
        "session_ttl_days": round(SESSION_TTL / 86400, 2),
        "https": _https_enabled(),
        "guest_plugins": app_settings.get_guest_plugins(),
        "all_plugins": [{"id": p.id, "name": p.name} for p in registry.all_plugins()],
        "settings_file": app_settings.settings_file_path(),
    })


@app.post("/settings")
async def settings_post(request: Request) -> Response:
    """Сохранить настройки (только админ). Порт/TTL/HTTPS — после перезапуска;
    пароль и гостевые разделы — сразу."""
    if not sessions.is_admin(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"error": "нужен админ-доступ"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "некорректный запрос"}, status_code=400)

    to_save: dict = {}
    messages: list[str] = []

    # Порт
    if "port" in body and body["port"] not in (None, ""):
        try:
            p = int(body["port"])
            if not (1 <= p <= 65535):
                raise ValueError
            to_save["port"] = p
            messages.append("порт сохранён (применится после перезапуска)")
        except (TypeError, ValueError):
            return JSONResponse({"error": "порт должен быть числом 1–65535"}, status_code=400)

    # TTL сессии (в днях из интерфейса)
    if "session_ttl_days" in body and body["session_ttl_days"] not in (None, ""):
        try:
            days = float(body["session_ttl_days"])
            ttl = days * 86400
            if not (300 <= ttl <= 90 * 24 * 3600):
                return JSONResponse({"error": "срок сессии: от 0.01 до 90 дней"}, status_code=400)
            to_save["session_ttl"] = ttl
            messages.append("срок сессии сохранён (применится после перезапуска)")
        except (TypeError, ValueError):
            return JSONResponse({"error": "срок сессии должен быть числом"}, status_code=400)

    # Смена пароля: нужен текущий пароль + новый
    new_pw = body.get("new_password")
    if new_pw:
        cur_pw = body.get("current_password", "")
        if not verify_password(cur_pw):
            return JSONResponse({"error": "текущий пароль неверный"}, status_code=403)
        if len(str(new_pw)) < 3:
            return JSONResponse({"error": "новый пароль слишком короткий"}, status_code=400)
        new_hash = hash_password(str(new_pw))
        to_save["password_hash"] = new_hash
        set_password_hash(new_hash)         # применяем сразу
        messages.append("пароль изменён")

    # HTTPS вкл/выкл
    if "https" in body:
        want_https = bool(body["https"])
        if want_https:
            from core import tls
            cert, key, cmsg = tls.ensure_cert()
            if not (cert and key):
                return JSONResponse({"error": f"HTTPS недоступен: {cmsg}"}, status_code=400)
            to_save["https"] = True
            messages.append("HTTPS включён (применится после перезапуска)")
        else:
            to_save["https"] = False
            messages.append("HTTPS выключен (применится после перезапуска)")

    # Гостевые разделы (галочки в админке)
    if "guest_plugins" in body and isinstance(body["guest_plugins"], list):
        loaded = {p.id for p in registry.all_plugins()}
        sel = [str(x) for x in body["guest_plugins"] if str(x) in loaded]
        to_save["guest_plugins"] = sel
        messages.append("гостевые разделы обновлены")

    if not to_save:
        return JSONResponse({"error": "нет изменений"}, status_code=400)

    if not app_settings.set_many(to_save):
        return JSONResponse({"error": "не удалось сохранить настройки на диск"},
                            status_code=500)
    log.info("Настройки обновлены: %s", ", ".join(to_save.keys()))
    return JSONResponse({"ok": True, "messages": messages})


# --------------------------------------------------------------------------- #
#                          ГЛАВНАЯ СТРАНИЦА / СТАТИКА                          #
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Дашборд. Если нет сессии — создаём гостевую автоматически (вход без
    пароля сразу в гостевой режим). Админ-доступ — через ввод пароля в UI."""
    if _authed(request):
        return HTMLResponse(content=_render_shell())
    # Нет сессии — выдаём гостевую и показываем дашборд в гостевом режиме.
    token = sessions.create(role="guest")
    resp = HTMLResponse(content=_render_shell())
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_TTL), httponly=True, samesite="lax",
    )
    return resp


@app.get("/manifest.json")
async def manifest() -> Response:
    return Response(content=MANIFEST_JSON, media_type="application/manifest+json")


# Иконки приложения (PWA + добавление на домашний экран iOS).
def _icon_response(name: str) -> Response:
    p = WEB_DIR / name
    if not p.exists():
        return Response(status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon-192.png")
async def icon_192() -> Response:
    return _icon_response("icon-192.png")


@app.get("/icon-512.png")
async def icon_512() -> Response:
    return _icon_response("icon-512.png")


@app.get("/apple-touch-icon.png")
async def apple_touch_icon() -> Response:
    # iOS запрашивает этот путь автоматически — отдаём 180×180.
    return _icon_response("icon-180.png")


@app.get("/apple-touch-icon-precomposed.png")
async def apple_touch_icon_pre() -> Response:
    return _icon_response("icon-180.png")


# --------------------------------------------------------------------------- #
#                               WEBSOCKET                                     #
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # Защита: к WS пускаем только с валидной сессией
    if not sessions.valid(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=4401)
        return

    await hub.connect(websocket)

    async def heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await websocket.send_text(json.dumps({"type": "ping"}))
        except Exception:  # noqa: BLE001
            pass

    hb_task = asyncio.create_task(heartbeat())
    try:
        while True:
            raw = await websocket.receive_text()
            sessions.touch(websocket.cookies.get(SESSION_COOKIE))
            await hub.route_inbound(websocket, raw)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("Ошибка в WebSocket")
    finally:
        hb_task.cancel()
        hub.disconnect(websocket)   # идемпотентно; гарантируем очистку всегда


if __name__ == "__main__":
    ssl_kwargs = {}
    scheme = "http"
    if _https_enabled():
        from core import tls
        cert, key, msg = tls.ensure_cert()
        if cert and key:
            ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
            scheme = "https"
            log.info("HTTPS включён (%s)", msg)
        else:
            log.warning("HTTPS запрошен, но недоступен: %s. Запускаюсь по HTTP.", msg)
    else:
        log.info("HTTPS выключен в настройках — работаю по HTTP. "
                 "Включить: настройки → Сервер → HTTPS → Сохранить → перезапустить.")
    log.info("Схема подключения: %s://%s:%d", scheme, SERVER_IP, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", **ssl_kwargs)
