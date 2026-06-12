"""
Состояние и общие синглтоны WinWave.

Этот модуль — «хаб»: и smtc_worker, и yandex_music, и commands берут отсюда
media_state / manager / history и общий httpx-клиент. Так разрывается
циклический импорт (модули не тянут друг друга, а тянут общий state).
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import WebSocket

from .media_config import (
    PORT,
    TRACK_HISTORY_MAX,
    SEARCH_HISTORY_MAX,
)

log = logging.getLogger("winwave")

# Живой флаг доступности API Яндекса. Мутируется из yandex_music, читается
# из MediaState.as_payload. Через словарь (а не голый bool), чтобы значение
# было общим между модулями, а не копией при импорте.
api_state = {"reachable": True}

class ConnectionManager:
    """
    Адаптер к WebSocket-хабу Helm.

    Раньше WinWave сам держал соединения и рассылал по ним. Теперь единственным
    владельцем соединений является ядро Helm (core.hub), а этот класс — тонкая
    прослойка: broadcast_json уходит в hub.broadcast('media', ...). Так все
    прежние вызовы manager.broadcast_json(...) по всему плагину продолжают
    работать без правок — меняется только то, что под капотом.
    """

    async def connect(self, websocket: WebSocket) -> None:
        # Соединениями управляет ядро Helm; здесь ничего не делаем.
        return None

    def disconnect(self, websocket: WebSocket) -> None:
        return None

    async def broadcast_json(self, payload: dict) -> None:
        """Разослать всем клиентам через хаб Helm от имени плагина media."""
        from core.hub import hub
        await hub.broadcast("media", payload)

    @property
    def has_clients(self) -> bool:
        from core.hub import hub
        return len(hub.connections) > 0


# --------------------------------------------------------------------------- #
#                          СОСТОЯНИЕ МЕДИАСЕССИИ SMTC                          #
# --------------------------------------------------------------------------- #



class MediaState:
    """Текущее состояние воспроизведения, агрегированное из SMTC."""

    def __init__(self) -> None:
        self.title: str = ""
        self.artist: str = ""
        self.album: str = ""
        self.app_id: str = ""
        self.is_playing: bool = False
        self.cover_bytes: Optional[bytes] = None
        self.cover_version: int = 0
        # Тайминги трека, секунды
        self.position: float = 0.0
        self.duration: float = 0.0
        # Лайк-статус и числовой ID трека в Яндексе (для лайка/проверки)
        self.is_liked: bool = False
        self.yandex_track_id: str = ""

    def as_payload(self, server_ip: str) -> dict:
        """Полный JSON-объект для отправки в браузер при смене трека."""
        from .audio_control import volume_controller
        vol = volume_controller.get_level()
        return {
            "type": "track",
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "appId": self.app_id,
            "isPlaying": self.is_playing,
            "isYandex": ("yandex" in self.app_id.lower()
                         or "яндекс" in self.app_id.lower()),
            # Относительный URL: браузер сам подставит протокол и хост страницы,
            # поэтому работает и по http, и по https (без mixed-content блокировки).
            "coverUrl": f"/current_cover.jpg?v={self.cover_version}",
            "position": round(self.position, 1),
            "duration": round(self.duration, 1),
            "volume": vol if vol is not None else -1,
            "volumeAvailable": volume_controller.available,
            "isMuted": bool(volume_controller.get_muted()),
            "isLiked": self.is_liked,
            "apiReachable": api_state["reachable"],
        }

    def as_progress(self) -> dict:
        """Лёгкий тик прогресса (без обложки и метаданных)."""
        from .audio_control import volume_controller
        vol = volume_controller.get_level()
        return {
            "type": "progress",
            "position": round(self.position, 1),
            "duration": round(self.duration, 1),
            "isPlaying": self.is_playing,
            "volume": vol if vol is not None else -1,
            "volumeAvailable": volume_controller.available,
        }




media_state = MediaState()
manager = ConnectionManager()


class HistoryStore:
    """
    История в памяти сервера: последние сыгранные треки и поисковые запросы.

    Треки хранятся как список словарей (новейший — первым). Обложка каждого
    трека кэшируется в байтах и отдаётся отдельным эндпоинтом по id, чтобы
    превью в истории работало независимо от текущего трека.
    """

    def __init__(self) -> None:
        self.tracks: list[dict] = []        # новейший первым
        self.searches: list[str] = []       # новейший первым
        self._cover_bytes: dict[str, bytes] = {}  # trackKey -> JPEG-байты
        self._seq: int = 0                  # счётчик для уникальных ключей

    # ---- треки ----
    @staticmethod
    def _track_key(artist: str, title: str) -> str:
        return f"{(artist or '').strip().lower()}|{(title or '').strip().lower()}"

    def add_track(self, artist: str, title: str,
                  cover: Optional[bytes]) -> None:
        """Добавить трек в историю (схлопывая повтор подряд идущего трека)."""
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title and not artist:
            return
        key = self._track_key(artist, title)

        # Если этот же трек уже на вершине истории — не дублируем,
        # только обновим обложку, если раньше её не было.
        if self.tracks and self.tracks[0].get("key") == key:
            if cover is not None and not self._cover_bytes.get(key):
                self._cover_bytes[key] = cover
                self.tracks[0]["coverVersion"] = self._next_seq()
            return

        # Уберём прошлые вхождения этого трека (чтобы он всплыл наверх один раз)
        self.tracks = [t for t in self.tracks if t.get("key") != key]

        if cover is not None:
            self._cover_bytes[key] = cover

        self.tracks.insert(0, {
            "key": key,
            "title": title,
            "artist": artist,
            "coverVersion": self._next_seq() if cover is not None else 0,
        })

        # Обрезаем по лимиту и чистим осиротевшие обложки
        if len(self.tracks) > TRACK_HISTORY_MAX:
            removed = self.tracks[TRACK_HISTORY_MAX:]
            self.tracks = self.tracks[:TRACK_HISTORY_MAX]
            live_keys = {t["key"] for t in self.tracks}
            for t in removed:
                if t["key"] not in live_keys:
                    self._cover_bytes.pop(t["key"], None)

    def update_track_cover(self, artist: str, title: str,
                           cover: Optional[bytes]) -> bool:
        """
        Обновить обложку уже добавленного трека (обложка приходит позже
        метаданных). Возвращает True, если что-то изменилось.
        """
        if cover is None:
            return False
        key = self._track_key(artist, title)
        for t in self.tracks:
            if t.get("key") == key:
                self._cover_bytes[key] = cover
                t["coverVersion"] = self._next_seq()
                return True
        return False

    def cover_bytes(self, key: str) -> Optional[bytes]:
        return self._cover_bytes.get(key)

    def tracks_payload(self, server_ip: str) -> list[dict]:
        """Список треков для фронтенда (без внутреннего ключа в чистом виде)."""
        out: list[dict] = []
        for t in self.tracks:
            cv = t.get("coverVersion", 0)
            cover_url = ""
            if cv:
                # Ключ кодируем в URL через quote — он содержит '|' и пробелы.
                # Относительный URL — работает и по http, и по https.
                cover_url = (
                    f"/history_cover?k={quote(t['key'])}&v={cv}"
                )
            out.append({
                "title": t["title"],
                "artist": t["artist"],
                "coverUrl": cover_url,
            })
        return out

    # ---- поиск ----
    def add_search(self, query: str) -> None:
        query = (query or "").strip()
        if not query:
            return
        low = query.lower()
        self.searches = [s for s in self.searches if s.lower() != low]
        self.searches.insert(0, query)
        if len(self.searches) > SEARCH_HISTORY_MAX:
            self.searches = self.searches[:SEARCH_HISTORY_MAX]

    def searches_payload(self) -> list[str]:
        return list(self.searches)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq




history = HistoryStore()


def _detect_server_ip() -> str:
    """Определить локальный IP сервера (для формирования coverUrl)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        sock.close()


SERVER_IP: str = _detect_server_ip()


def _history_payload() -> dict:
    """WebSocket-сообщение с историей треков и поисковых запросов."""
    return {
        "type": "history",
        "tracks": history.tracks_payload(SERVER_IP),
        "searches": history.searches_payload(),
    }


def _apps_payload() -> dict:
    """WebSocket-сообщение со списком приложений со звуком (поканальная громкость)."""
    from .audio_control import app_volume_controller
    return {
        "type": "apps",
        "available": app_volume_controller.available,
        "apps": app_volume_controller.list_apps(),
    }

_cover_cache: dict[str, Optional[bytes]] = {}
_COVER_CACHE_LIMIT: int = 200

# Общий httpx-клиент для запросов к API Яндекса (обложки/поиск через httpx).
# Один долгоживущий клиент держит TCP/TLS-соединение тёплым (keep-alive), что
# убирает повторные DNS+handshake на каждый трек — главный источник «то быстро,
# то 20 секунд». Создаётся лениво и переиспользуется.
_shared_http: Optional[httpx.AsyncClient] = None

# Таймауты разнесены по фазам: соединиться надо быстро (если висим на connect —
# сеть/прокси не отвечают, нет смысла ждать), а на чтение даём чуть больше.
_HTTP_TIMEOUT = httpx.Timeout(connect=2.0, read=4.0, write=2.0, pool=2.0)
# Пул keep-alive соединений, чтобы повторные запросы шли по уже открытому каналу.
_HTTP_LIMITS = httpx.Limits(max_keepalive_connections=8, keepalive_expiry=60.0)


def _get_http() -> httpx.AsyncClient:
    """Вернуть общий httpx-клиент, создав его при первом обращении."""
    global _shared_http
    if _shared_http is None or _shared_http.is_closed:
        _shared_http = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=_HTTP_LIMITS,
            headers={"User-Agent": "WinWave/3.7"},
        )
    return _shared_http


async def _close_http() -> None:
    """Закрыть общий httpx-клиент при остановке сервера."""
    global _shared_http
    if _shared_http is not None and not _shared_http.is_closed:
        try:
            await _shared_http.aclose()
        except Exception:  # noqa: BLE001
            pass
    _shared_http = None
