"""
Интеграция с Яндекс Музыкой: обложки, поиск треков, открытие/добавление
трека и лайки. Работает через официальную библиотеку yandex-music, если она
установлена, иначе откатывается на ручные httpx-запросы к приватному API.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional
from urllib.parse import quote

import httpx

from .media_config import (
    YANDEX_MUSIC_TOKEN,
    YANDEX_API_BASE,
    COVER_TIMEOUT,
)
from .state import (
    media_state,
    manager,
    SERVER_IP,
    _cover_cache,
    _COVER_CACHE_LIMIT,
    _get_http,
)

log = logging.getLogger("winwave")

# Библиотека yandex-music (опциональная).
try:
    from yandex_music import ClientAsync as _YMClientAsync
    _YM_LIB_AVAILABLE = True
except Exception:  # noqa: BLE001
    _YMClientAsync = None
    _YM_LIB_AVAILABLE = False

_ym_client = None
_ym_init_tried = False
_ym_init_lock: Optional[asyncio.Lock] = None


async def _get_ym_client():
    """Вернуть инициализированный ClientAsync или None (с откатом на httpx)."""
    global _ym_client, _ym_init_tried, _ym_init_lock
    if not _YM_LIB_AVAILABLE or not YANDEX_MUSIC_TOKEN:
        return None
    if _ym_client is not None:
        return _ym_client
    if _ym_init_tried and _ym_client is None:
        return None  # уже пробовали и не вышло — не долбимся повторно
    if _ym_init_lock is None:
        _ym_init_lock = asyncio.Lock()
    async with _ym_init_lock:
        if _ym_client is not None:
            return _ym_client
        if _ym_init_tried:
            return None
        _ym_init_tried = True
        try:
            client = _YMClientAsync(YANDEX_MUSIC_TOKEN)
            await asyncio.wait_for(client.init(), timeout=8.0)
            _ym_client = client
            log.info("Библиотека yandex-music инициализирована")
        except Exception as exc:  # noqa: BLE001
            log.warning("yandex-music init не удался (откат на httpx): %r", exc)
            _ym_client = None
    return _ym_client


# Доступность API Яндекса (для индикации на телефоне). Обновляется при
# каждой попытке запроса обложки/поиска. Меняется на False только при
# сетевой ошибке, на True — при успешном ответе.


def _set_api_reachable(ok: bool) -> None:
    """Обновить флаг доступности API и, при изменении, сообщить клиентам."""
    from .state import api_state
    if ok != api_state["reachable"]:
        api_state["reachable"] = ok
        log.info("API Яндекса: %s", "доступен" if ok else "НЕдоступен")
        # Рассылаем обновлённое состояние, чтобы телефон показал/убрал плашку
        try:
            asyncio.create_task(
                manager.broadcast_json(media_state.as_payload(SERVER_IP))
            )
        except RuntimeError:
            pass  # нет активного loop — не критично


def _cover_cache_key(artist: str, title: str) -> str:
    return f"{artist.strip().lower()}|{title.strip().lower()}"


def _match_score(track: dict, artist: str, title: str) -> int:
    """
    Простая оценка совпадения результата поиска с тем, что играет.
    Чем выше — тем ближе. Сравниваем название и исполнителей без регистра.
    """
    score = 0
    t_title = (track.get("title") or "").strip().lower()
    if t_title and t_title == title.strip().lower():
        score += 3
    elif t_title and title.strip().lower() in t_title:
        score += 1

    artists = track.get("artists") or []
    names = " ".join((a.get("name") or "") for a in artists).lower()
    want = artist.strip().lower()
    if want and want in names:
        score += 2
    # Небольшой бонус за наличие самой обложки
    if track.get("coverUri"):
        score += 1
    return score




async def _fetch_yandex_cover(artist: str, title: str) -> Optional[bytes]:
    """
    Найти обложку трека через API Яндекс Музыки по исполнителю и названию.

    Использует публичный поиск (токен не обязателен). Среди результатов
    выбирает наиболее подходящий по названию/исполнителю, а не просто первый.
    Результат кэшируется в памяти, чтобы повторные обращения к одному треку
    (например при перемотке) не дёргали API заново.
    """
    query = f"{artist} {title}".strip()
    if not query:
        return None

    key = _cover_cache_key(artist, title)
    if key in _cover_cache:
        return _cover_cache[key]  # может быть и None — "искали, не нашли"

    headers = {}
    if YANDEX_MUSIC_TOKEN:
        headers["Authorization"] = f"OAuth {YANDEX_MUSIC_TOKEN}"

    cover_bytes: Optional[bytes] = None
    fetch_failed = False
    try:
        client = _get_http()   # общий тёплый клиент, без пересоздания
        search = await client.get(
            f"{YANDEX_API_BASE}/search",
            params={"type": "track", "page": 0, "text": query},
            headers=headers,
        )
        search.raise_for_status()
        result = search.json().get("result", {})
        tracks = (result.get("tracks") or {}).get("results") or []

        if tracks:
            # Выбираем наиболее похожий результат, а не просто первый
            best = max(tracks, key=lambda t: _match_score(t, artist, title))

            cover_uri = best.get("coverUri")
            if not cover_uri:
                albums = best.get("albums") or []
                if albums:
                    cover_uri = albums[0].get("coverUri")

            if cover_uri:
                url = "https://" + cover_uri.replace("%%", "400x400")
                img = await client.get(url)
                img.raise_for_status()
                cover_bytes = img.content
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить обложку из Яндекс API: %r", exc)
        cover_bytes = None
        fetch_failed = True

    # Отмечаем доступность API Яндекса (для индикации на телефоне)
    _set_api_reachable(not fetch_failed)

    # Кэшируем результат, НО не кэшируем неудачу из-за сетевой ошибки —
    # иначе временный сбой навсегда оставит трек без обложки.
    if not fetch_failed:
        if len(_cover_cache) >= _COVER_CACHE_LIMIT:
            _cover_cache.clear()  # простой сброс при переполнении
        _cover_cache[key] = cover_bytes
    return cover_bytes


async def _yandex_search_tracks(query: str, limit: int = 20) -> list[dict]:
    """
    Поиск треков. Сначала через библиотеку yandex-music, при неудаче —
    через ручной httpx-запрос. Возвращает список словарей для фронтенда:
    {trackId, albumId, title, artist, coverUrl, durationMs}.
    """
    query = (query or "").strip()
    if not query:
        return []

    client = await _get_ym_client()
    if client is not None:
        try:
            res = await asyncio.wait_for(
                client.search(query, type_="track"), timeout=6.0)
            tracks = (res.tracks.results if res and res.tracks else None) or []
            out: list[dict] = []
            for t in tracks[:limit]:
                if getattr(t, "available", True) is False:
                    continue
                tid = getattr(t, "id", None)
                if not tid:
                    continue
                artists = ", ".join(a.name for a in (t.artists or []) if a.name)
                albums = t.albums or []
                album_id = albums[0].id if albums else ""
                try:
                    cover_url = ("https://" + t.cover_uri.replace("%%", "100x100")
                                 if t.cover_uri else "")
                except Exception:  # noqa: BLE001
                    cover_url = ""
                out.append({
                    "trackId": str(tid),
                    "albumId": str(album_id) if album_id else "",
                    "title": t.title or "",
                    "artist": artists or "Неизвестный исполнитель",
                    "coverUrl": cover_url,
                    "durationMs": t.duration_ms or 0,
                })
            _set_api_reachable(True)
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("Поиск через библиотеку не удался (откат): %r", exc)

    return await _yandex_search_tracks_httpx(query, limit)


async def _yandex_search_tracks_httpx(query: str, limit: int = 20) -> list[dict]:
    """Резервный поиск треков через ручной httpx-запрос (без библиотеки)."""
    headers = {}
    if YANDEX_MUSIC_TOKEN:
        headers["Authorization"] = f"OAuth {YANDEX_MUSIC_TOKEN}"

    out: list[dict] = []
    try:
        client = _get_http()
        resp = await client.get(
            f"{YANDEX_API_BASE}/search",
            params={"type": "track", "page": 0, "text": query},
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        tracks = (result.get("tracks") or {}).get("results") or []

        for t in tracks[:limit]:
            track_id = t.get("id")
            if not track_id:
                continue
            # available=False — трек недоступен (регион/лицензия), пропускаем
            if t.get("available") is False:
                continue
            artists = ", ".join(
                (a.get("name") or "") for a in (t.get("artists") or [])
            ).strip(", ")
            albums = t.get("albums") or []
            album_id = albums[0].get("id") if albums else ""

            cover_uri = t.get("coverUri")
            if not cover_uri and albums:
                cover_uri = albums[0].get("coverUri")
            cover_url = ""
            if cover_uri:
                cover_url = "https://" + cover_uri.replace("%%", "100x100")

            out.append({
                "trackId": str(track_id),
                "albumId": str(album_id) if album_id else "",
                "title": t.get("title") or "",
                "artist": artists or "Неизвестный исполнитель",
                "coverUrl": cover_url,
                "durationMs": t.get("durationMs") or 0,
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("Поиск треков не удался: %r", exc)
    return out


def _yandex_protocol_registered() -> bool:
    """Проверить, зарегистрирован ли протокол yandexmusic:// в Windows."""
    try:
        import winreg
        for root in (winreg.HKEY_CLASSES_ROOT, winreg.HKEY_CURRENT_USER):
            try:
                key_path = ("yandexmusic" if root == winreg.HKEY_CLASSES_ROOT
                            else r"Software\Classes\yandexmusic")
                with winreg.OpenKey(root, key_path) as k:
                    # Должен быть подключ shell\open\command
                    winreg.OpenKey(k, r"shell\open\command")
                    return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


def _yandex_track_urls(track_id: str, album_id: str) -> list[tuple[str, str]]:
    """Ссылки для открытия трека. proto-album-track открывает трек в приложении."""
    urls: list[tuple[str, str]] = []
    if album_id:
        urls.append(("proto-album-track",
                     f"yandexmusic://album/{album_id}/track/{track_id}"))
    urls.append(("proto-track", f"yandexmusic://track/{track_id}"))
    urls.append(("web-track",
                 f"https://music.yandex.ru/track/{track_id}?play=true"))
    return urls


def _open_yandex_track(track_id: str, album_id: str) -> bool:
    """
    Открыть трек в десктопном приложении Яндекса. Приложение откроет альбом
    с выбранным треком в боковой панели — запуск пользователь делает сам
    кнопкой «Слушать» (надёжного способа автозапуска через ссылку у
    приложения нет).
    """
    if not track_id:
        return False

    candidates = _yandex_track_urls(track_id, album_id)
    name, url = candidates[0]
    try:
        os.startfile(url)  # type: ignore[attr-defined]
        log.info("Открываю трек [%s]: %s", name, url)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Не удалось открыть ссылку трека [%s]: %r", name, exc)
        return False


async def _yandex_add_track(track_id: str, album_id: str) -> bool:
    """Добавить трек в коллекцию (лайк) по его ID — без воспроизведения."""
    if not YANDEX_MUSIC_TOKEN or not track_id:
        return False

    global _yandex_liked_at
    # Сначала пробуем библиотеку (сама подставляет user_id)
    client = await _get_ym_client()
    if client is not None:
        try:
            await asyncio.wait_for(
                client.users_likes_tracks_add(str(track_id)), timeout=8.0)
            _yandex_liked_at = 0.0  # сброс кэша лайков
            log.info("Трек добавлен в коллекцию (lib): %s", track_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("add_track через библиотеку не удался (откат): %r", exc)

    headers = {"Authorization": f"OAuth {YANDEX_MUSIC_TOKEN}"}
    track_ref = f"{track_id}:{album_id}" if album_id else str(track_id)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            uid = await _yandex_uid(client, headers)
            if uid is None:
                return False
            resp = await client.post(
                f"{YANDEX_API_BASE}/users/{uid}/likes/tracks/add-multiple",
                headers=headers, data={"track-ids": track_ref},
            )
            resp.raise_for_status()
            _yandex_liked_at = 0.0
            log.info("Трек добавлен в коллекцию: %s", track_ref)
            return True
    except Exception as exc:  # noqa: BLE001
        log.error("Не удалось добавить трек в коллекцию: %r", exc)
        return False


async def _yandex_like_current_track() -> None:
    """Поставить лайк текущему треку через приватный API Яндекс Музыки."""
    if not YANDEX_MUSIC_TOKEN:
        log.warning("YANDEX_MUSIC_TOKEN не задан — лайк невозможен")
        return

    app_id = media_state.app_id.lower()
    if "yandex" not in app_id and "яндекс" not in app_id:
        log.info("Активный плеер не Яндекс — лайк пропущен")
        return

    headers = {"Authorization": f"OAuth {YANDEX_MUSIC_TOKEN}"}
    query = f"{media_state.artist} {media_state.title}".strip()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            uid = await _yandex_uid(client, headers)
            if uid is None:
                return

            track_id = media_state.yandex_track_id
            album_id = ""
            # Если ID ещё не определён — ищем трек
            if not track_id:
                track_id, album_id = await _yandex_find_track_id(
                    client, headers, query
                )
            if not track_id:
                log.warning("Трек не найден в Яндекс API для лайка: %s", query)
                return

            liked_set = await _yandex_liked_set(client, headers, uid)

            # Направление тоггла берём из текущего состояния сердечка
            # (media_state.is_liked), а не только из кэша — так надёжнее
            # сразу после предыдущего нажатия.
            currently_liked = (str(track_id) in liked_set) or media_state.is_liked

            if currently_liked:
                endpoint = "remove"
                action = "снят"
                new_state = False
                # для remove Яндекс ждёт просто track-id (без альбома)
                track_ref = str(track_id)
            else:
                endpoint = "add-multiple"
                action = "поставлен"
                new_state = True
                track_ref = f"{track_id}:{album_id}" if album_id else str(track_id)

            resp = await client.post(
                f"{YANDEX_API_BASE}/users/{uid}/likes/tracks/{endpoint}",
                headers=headers, data={"track-ids": track_ref},
            )
            resp.raise_for_status()

            # Обновляем локальное состояние и кэш лайков
            media_state.is_liked = new_state
            if new_state:
                liked_set.add(str(track_id))
            else:
                liked_set.discard(str(track_id))
            log.info("Лайк %s: %s — %s",
                     action, media_state.artist, media_state.title)

            # Сообщаем клиентам новое состояние сердечка
            await manager.broadcast_json(media_state.as_payload(SERVER_IP))
    except Exception as exc:  # noqa: BLE001
        log.error("Не удалось переключить лайк: %r", exc)


# --- Вспомогательные функции для лайков Яндекса -------------------------------

_yandex_uid_cache: Optional[str] = None
# Кэш множества лайкнутых track_id + время последнего обновления
_yandex_liked_cache: set[str] = set()
_yandex_liked_at: float = 0.0
_YANDEX_LIKED_TTL: float = 60.0  # как часто обновлять список лайков, секунды


async def _yandex_uid(client: httpx.AsyncClient, headers: dict) -> Optional[str]:
    """Получить (и закэшировать) uid пользователя Яндекса."""
    global _yandex_uid_cache
    if _yandex_uid_cache:
        return _yandex_uid_cache
    try:
        resp = await client.get(
            f"{YANDEX_API_BASE}/account/status", headers=headers
        )
        resp.raise_for_status()
        _yandex_uid_cache = str(resp.json()["result"]["account"]["uid"])
        return _yandex_uid_cache
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить uid Яндекса: %r", exc)
        return None


async def _yandex_find_track_id(
    client: httpx.AsyncClient, headers: dict, query: str
) -> tuple[str, str]:
    """Найти трек через поиск, вернуть (track_id, album_id) строками."""
    try:
        resp = await client.get(
            f"{YANDEX_API_BASE}/search",
            params={"type": "track", "page": 0, "text": query},
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        tracks = (result.get("tracks") or {}).get("results") or []
        if not tracks:
            return "", ""
        best = max(tracks, key=lambda t: _match_score(
            t, media_state.artist, media_state.title))
        track_id = str(best.get("id") or "")
        albums = best.get("albums") or []
        album_id = str(albums[0].get("id")) if albums else ""
        return track_id, album_id
    except Exception as exc:  # noqa: BLE001
        log.warning("Поиск трека для лайка не удался: %r", exc)
        return "", ""


async def _yandex_liked_set(
    client: httpx.AsyncClient, headers: dict, uid: str
) -> set[str]:
    """
    Множество ID лайкнутых треков пользователя (кэшируется на _YANDEX_LIKED_TTL).
    Сначала пробует библиотеку yandex-music, при неудаче — ручной httpx.
    """
    global _yandex_liked_cache, _yandex_liked_at
    import time
    now = time.monotonic()
    if _yandex_liked_cache and now - _yandex_liked_at < _YANDEX_LIKED_TTL:
        return _yandex_liked_cache

    # Путь через библиотеку (сама подставляет user_id, отдаёт готовые объекты)
    ym = await _get_ym_client()
    if ym is not None:
        try:
            tracks_list = await asyncio.wait_for(
                ym.users_likes_tracks(), timeout=8.0)
            ids = set()
            for t in (tracks_list.tracks if tracks_list else []) or []:
                tid = getattr(t, "id", None)
                if tid is not None:
                    ids.add(str(tid))
            _yandex_liked_cache = ids
            _yandex_liked_at = now
            log.debug("[лайки] загружено %d лайкнутых треков (lib)", len(ids))
            return ids
        except Exception as exc:  # noqa: BLE001
            log.warning("Список лайков через библиотеку не удался (откат): %r", exc)

    try:
        resp = await client.get(
            f"{YANDEX_API_BASE}/users/{uid}/likes/tracks", headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        library = data.get("result", {}).get("library", {})
        tracks = library.get("tracks", []) or []
        ids = set()
        for t in tracks:
            tid = t.get("id")
            if tid is not None:
                ids.add(str(tid))
        _yandex_liked_cache = ids
        _yandex_liked_at = now
        log.debug("[лайки] загружено %d лайкнутых треков", len(ids))
        return ids
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить список лайков: %r", exc)
        return _yandex_liked_cache  # вернём что было (возможно пусто)


async def _yandex_check_liked(artist: str, title: str) -> tuple[bool, str]:
    """
    Проверить, лайкнут ли трек. Возвращает (is_liked, track_id).
    Используется при смене трека, чтобы сразу зажечь/погасить сердечко.
    """
    if not YANDEX_MUSIC_TOKEN:
        return False, ""
    headers = {"Authorization": f"OAuth {YANDEX_MUSIC_TOKEN}"}
    query = f"{artist} {title}".strip()
    if not query:
        return False, ""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            uid = await _yandex_uid(client, headers)
            if uid is None:
                return False, ""
            track_id, _ = await _yandex_find_track_id(client, headers, query)
            if not track_id:
                log.debug("[лайки] трек не найден поиском: %s", query)
                return False, ""
            liked = await _yandex_liked_set(client, headers, uid)
            is_liked = str(track_id) in liked
            log.debug("[лайки] «%s» id=%s в коллекции: %s",
                      title, track_id, is_liked)
            return is_liked, track_id
    except Exception:  # noqa: BLE001
        return False, ""


async def _update_like_status(artist: str, title: str) -> None:
    """Определить лайк-статус трека и разослать его клиентам (в фоне)."""
    is_liked, track_id = await _yandex_check_liked(artist, title)
    # Если трек уже сменился, пока мы проверяли — не трогаем
    if (artist, title) != (media_state.artist, media_state.title):
        return
    media_state.is_liked = is_liked
    media_state.yandex_track_id = track_id
    await manager.broadcast_json(media_state.as_payload(SERVER_IP))
