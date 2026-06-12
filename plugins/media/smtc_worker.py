"""
Работа с системными медиаданными Windows через SMTC (WinRT):
слушатель сессий, обновление трека/обложки/таймлайна, перемотка и тикер
прогресса. Здесь же — единственная точка, дергающая медиаклавиши.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSession as MediaSession,
)
from winsdk.windows.storage.streams import (
    DataReader,
    IRandomAccessStreamWithContentType,
)

from .media_config import (
    PROGRESS_TICK_INTERVAL,
    TRACK_DEBOUNCE,
    COVER_TIMEOUT,
    YANDEX_MUSIC_TOKEN,
)
from .state import (
    media_state,
    manager,
    history,
    SERVER_IP,
    _history_payload,
)
from .yandex_music import (
    _fetch_yandex_cover,
    _update_like_status,
)

log = logging.getLogger("winwave")

async def _read_stream_bytes(
    stream: IRandomAccessStreamWithContentType,
) -> Optional[bytes]:
    """Безопасно прочитать IRandomAccessStream в bytes через DataReader."""
    try:
        size = stream.size
        if size == 0:
            return None
        reader = DataReader(stream)
        await reader.load_async(size)
        buffer = bytearray(size)
        reader.read_bytes(buffer)
        return bytes(buffer)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать поток обложки")
        return None


# --------------------------------------------------------------------------- #
#                          ОБНОВЛЕНИЕ ДАННЫХ ТРЕКА                             #
# --------------------------------------------------------------------------- #

def _to_seconds(value) -> float:
    """Привести значение таймлайна к секундам.

    winsdk может отдать timedelta (есть .total_seconds()) либо целое число
    "тиков" по 100 нс. Поддерживаем оба варианта.
    """
    try:
        return float(value.total_seconds())
    except AttributeError:
        try:
            return float(value) / 10_000_000.0
        except Exception:  # noqa: BLE001
            return 0.0


def _read_timeline(session: MediaSession) -> tuple[float, float]:
    """Прочитать позицию и длительность трека (секунды). При сбое — (0, 0)."""
    try:
        tl = session.get_timeline_properties()
        position = _to_seconds(tl.position)
        end = _to_seconds(tl.end_time)
        start = _to_seconds(tl.start_time)
        duration = max(0.0, end - start)
        return max(0.0, position), duration
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


# Кэш обложек Яндекса: ключ "исполнитель|трек" (нижний регистр) -> JPEG bytes.


async def _refresh_current_track(session: MediaSession) -> None:
    """Запросить свойства трека и обложку, обновить кэш, разослать клиентам."""
    try:
        props = await session.try_get_media_properties_async()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось получить media properties")
        return

    try:
        media_state.title = props.title or ""
        media_state.artist = props.artist or ""
        media_state.album = props.album_title or ""

        try:
            media_state.app_id = session.source_app_user_model_id or ""
        except Exception:  # noqa: BLE001
            media_state.app_id = ""

        # Статус воспроизведения
        try:
            playback = session.get_playback_info()
            media_state.is_playing = int(playback.playback_status) == 4
        except Exception:  # noqa: BLE001
            media_state.is_playing = False

        # Тайминги
        media_state.position, media_state.duration = _read_timeline(session)

        # Сбрасываем лайк-статус и ID при смене трека (определим заново ниже)
        media_state.is_liked = False
        media_state.yandex_track_id = ""

        # СНАЧАЛА — рассылаем метаданные сразу, не дожидаясь обложки.
        # Обложка может зависнуть (WinRT-поток или сеть), и она НЕ должна
        # блокировать обновление названия/исполнителя на телефоне.
        media_state.cover_version += 1
        log.info(
            "Трек: «%s — %s» [%s] play=%s %.0f/%.0fс",
            media_state.artist, media_state.title, media_state.app_id,
            media_state.is_playing, media_state.position, media_state.duration,
        )
        await manager.broadcast_json(media_state.as_payload(SERVER_IP))

        # Записываем трек в историю (обложка добавится ниже, когда загрузится).
        history.add_track(media_state.artist, media_state.title, None)
        await manager.broadcast_json(_history_payload())

        # Проверка лайка — в фоне, чтобы не задерживать (зажжёт сердечко, если
        # трек уже в коллекции). Только для Яндекса и при наличии токена.
        if YANDEX_MUSIC_TOKEN and (
            "yandex" in media_state.app_id.lower()
            or "яндекс" in media_state.app_id.lower()
        ):
            asyncio.create_task(_update_like_status(
                media_state.artist, media_state.title
            ))

        # ЗАТЕМ — обложка отдельно, с жёстким таймаутом. Если не успела —
        # просто остаётся без обложки, трек уже показан.
        artist_snapshot = media_state.artist
        title_snapshot = media_state.title
        try:
            cover = await asyncio.wait_for(
                _load_cover(props), timeout=COVER_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.debug("обложка не успела за %.0fс — пропускаем", COVER_TIMEOUT)
            cover = None
        except Exception:  # noqa: BLE001
            log.exception("ошибка загрузки обложки")
            cover = None

        # Если за время загрузки трек уже сменился — не перетираем чужую обложку
        if (media_state.artist, media_state.title) != (
            artist_snapshot, title_snapshot
        ):
            return

        if cover is not None:
            media_state.cover_bytes = cover
            media_state.cover_version += 1
            log.debug("обложка загружена, рассылаем")
            await manager.broadcast_json(media_state.as_payload(SERVER_IP))
            # Подвесим обложку и к записи в истории
            if history.update_track_cover(
                artist_snapshot, title_snapshot, cover
            ):
                await manager.broadcast_json(_history_payload())
        # Если обложки нет — НЕ трогаем cover_bytes (могла остаться валидной
        # с прошлого обновления того же трека); версия уже инкрементилась.
    except Exception:  # noqa: BLE001
        log.exception("Ошибка при обновлении данных трека")


async def _load_cover(props) -> Optional[bytes]:
    """
    Загрузить обложку максимально быстро: SMTC-thumbnail и поиск в API Яндекса
    запускаются ПАРАЛЛЕЛЬНО, берётся первый успешный результат. Так мы не ждём
    впустую зависающий на некоторых системах SMTC-thumbnail.
    """
    tasks = []

    # 1) SMTC-thumbnail (с коротким таймаутом — может зависнуть намертво)
    if props.thumbnail is not None:
        tasks.append(asyncio.create_task(
            _read_smtc_thumbnail_safe(props.thumbnail)
        ))

    # 2) API Яндекса (если играет Яндекс)
    if ("yandex" in media_state.app_id.lower()
            or "яндекс" in media_state.app_id.lower()):
        tasks.append(asyncio.create_task(
            _fetch_yandex_cover(media_state.artist, media_state.title)
        ))

    if not tasks:
        return None

    cover: Optional[bytes] = None
    try:
        # Ждём задачи по мере готовности, берём первый непустой результат
        for fut in asyncio.as_completed(tasks):
            result = await fut
            if result:
                cover = result
                break
    finally:
        # Отменяем оставшиеся незавершённые задачи
        for t in tasks:
            if not t.done():
                t.cancel()
    return cover


async def _read_smtc_thumbnail_safe(thumbnail) -> Optional[bytes]:
    """SMTC-thumbnail с собственным коротким таймаутом (часто зависает)."""
    try:
        return await asyncio.wait_for(
            _read_smtc_thumbnail(thumbnail), timeout=1.0
        )
    except Exception:  # noqa: BLE001
        return None


async def _read_smtc_thumbnail(thumbnail) -> Optional[bytes]:
    """Прочитать обложку из SMTC-thumbnail (может зависнуть — оборачивается в таймаут)."""
    stream = await thumbnail.open_read_async()
    return await _read_stream_bytes(stream)


async def _refresh_timeline_only(session: MediaSession) -> None:
    """Обновить только тайминги/статус и разослать лёгкий прогресс-тик."""
    try:
        playback = session.get_playback_info()
        media_state.is_playing = int(playback.playback_status) == 4
    except Exception:  # noqa: BLE001
        pass
    media_state.position, media_state.duration = _read_timeline(session)
    await manager.broadcast_json(media_state.as_progress())


async def _clear_state() -> None:
    """Сбросить состояние трека и сообщить клиентам, что ничего не играет."""
    media_state.title = ""
    media_state.artist = ""
    media_state.album = ""
    media_state.app_id = ""
    media_state.is_playing = False
    media_state.cover_bytes = None
    media_state.cover_version += 1
    media_state.position = 0.0
    media_state.duration = 0.0
    log.info("Состояние очищено — плеер закрыт / ничего не играет")
    await manager.broadcast_json(media_state.as_payload(SERVER_IP))


# --------------------------------------------------------------------------- #
#                       ПОДПИСКА НА СОБЫТИЯ SMTC                               #
# --------------------------------------------------------------------------- #


class SmtcListener:
    """Подписка на смену сессии, свойств медиа и таймлайна SMTC."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.manager: Optional[SessionManager] = None
        self.current_session: Optional[MediaSession] = None
        self._props_token = None
        self._playback_token = None
        self._timeline_token = None
        # Дебаунс через счётчик поколений (см. _schedule_refresh)
        self._generation: int = 0

    async def start(self) -> None:
        try:
            self.manager = await SessionManager.request_async()
            self.manager.add_current_session_changed(self._on_session_changed)
            log.info("SMTC SessionManager инициализирован")
            self._bind_current_session()
        except Exception:  # noqa: BLE001
            log.exception("SMTC недоступен — сервер продолжит работать без него")

    @property
    def session(self) -> Optional[MediaSession]:
        return self.current_session

    def _on_session_changed(self, *_args) -> None:
        log.info("Событие: current_session_changed")
        self._bind_current_session()

    def _on_properties_changed(self, session: MediaSession, *_args) -> None:
        self._schedule_refresh()

    def _on_playback_changed(self, session: MediaSession, *_args) -> None:
        self._schedule_refresh()

    def _on_timeline_changed(self, session: MediaSession, *_args) -> None:
        # Тайминги обновляем сразу (без дебаунса) — это лёгкая операция
        asyncio.run_coroutine_threadsafe(
            _refresh_timeline_only(session), self.loop
        )

    def _schedule_refresh(self) -> None:
        """
        Запланировать обновление. Дебаунс реализован ВНУТРИ корутины через
        счётчик поколений: каждое событие увеличивает счётчик, корутина ждёт
        TRACK_DEBOUNCE и выполняется, только если осталась самой свежей.
        Так пачка событий (включая «блик» сессии при смене трека) приводит
        к одному фактическому обновлению, и доставка не теряется.

        Колбэки приходят из WinRT-потока, поэтому планируем через loop.
        """
        self.loop.call_soon_threadsafe(self._bump_and_run)

    def _bump_and_run(self) -> None:
        self._generation += 1
        gen = self._generation
        self.loop.create_task(self._debounced_apply(gen))

    async def _debounced_apply(self, gen: int) -> None:
        # Ждём паузу; если за это время пришло событие новее — выходим
        await asyncio.sleep(TRACK_DEBOUNCE)
        if gen != self._generation:
            return
        # Перечитываем сессию заново на случай, если она появилась после
        # последнего события current_session_changed
        session = None
        if self.manager is not None:
            try:
                session = self.manager.get_current_session()
                self.current_session = session
            except Exception:  # noqa: BLE001
                log.exception("get_current_session упал")
        try:
            if session is not None:
                # Переподписка, если это новая сессия без активных токенов
                if self._props_token is None:
                    self._subscribe(session)
                await _refresh_current_track(session)
            else:
                await _clear_state()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в _debounced_apply")

    def _subscribe(self, session: MediaSession) -> None:
        """Подписаться на события конкретной сессии."""
        try:
            self._props_token = session.add_media_properties_changed(
                self._on_properties_changed
            )
            self._playback_token = session.add_playback_info_changed(
                self._on_playback_changed
            )
            try:
                self._timeline_token = session.add_timeline_properties_changed(
                    self._on_timeline_changed
                )
            except Exception:  # noqa: BLE001
                self._timeline_token = None
        except Exception:  # noqa: BLE001
            log.exception("Не удалось подписаться на сессию")

    def _bind_current_session(self) -> None:
        if self.manager is None:
            return
        try:
            # Отписываемся от старой сессии и сбрасываем токены
            if self.current_session is not None:
                for remover, token in (
                    ("remove_media_properties_changed", self._props_token),
                    ("remove_playback_info_changed", self._playback_token),
                    ("remove_timeline_properties_changed", self._timeline_token),
                ):
                    try:
                        if token is not None:
                            getattr(self.current_session, remover)(token)
                    except Exception:  # noqa: BLE001
                        pass
            self._props_token = None
            self._playback_token = None
            self._timeline_token = None

            session = self.manager.get_current_session()
            self.current_session = session
            if session is not None:
                self._subscribe(session)
            # В любом случае планируем реакцию: _debounced_apply сам перечитает
            # сессию после паузы и решит — обновить трек или очистить.
            self._schedule_refresh()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка привязки к текущей сессии")


smtc_listener: Optional[SmtcListener] = None


# --------------------------------------------------------------------------- #
#                          ПЕРЕМОТКА ТРЕКА (SEEK)                              #
# --------------------------------------------------------------------------- #

async def _seek_to(seconds: float) -> None:
    """
    Перемотать трек на указанную позицию (секунды).

    Поддерживается не всеми плеерами — Яндекс может игнорировать.
    """
    if smtc_listener is None or smtc_listener.session is None:
        return
    try:
        # API ожидает позицию в "тиках" по 100 нс
        ticks = int(seconds * 10_000_000)
        await smtc_listener.session.try_change_playback_position_async(ticks)
        log.info("Перемотка на %.0fс", seconds)
    except Exception:  # noqa: BLE001
        log.exception("Перемотка не поддерживается плеером")


# --------------------------------------------------------------------------- #
#                          ФОНОВЫЙ ТИКЕР ПРОГРЕССА                            #
# --------------------------------------------------------------------------- #

async def _progress_ticker() -> None:
    """
    Раз в секунду:
      1) подталкивает позицию вперёд (плавный прогресс между событиями SMTC);
      2) ПОЛЛИНГ-ФОЛЛБЭК: перечитывает текущую сессию и, если трек изменился
         (а событие почему-то не сработало), принудительно обновляет.
    Это страховка на случай, если WinRT-события доставляются ненадёжно.
    """
    last_seen: tuple[str, str] = ("", "")
    poll_counter = 0
    try:
        while True:
            await asyncio.sleep(PROGRESS_TICK_INTERVAL)
            poll_counter += 1

            # --- Поллинг текущего трека (страховка от потери событий) ---
            # Реже, чем прогресс (раз в ~3с), и с таймаутом, т.к. WinRT-вызов
            # на некоторых системах может зависнуть.
            if (poll_counter % 3 == 0
                    and smtc_listener is not None
                    and smtc_listener.manager is not None):
                try:
                    session = smtc_listener.manager.get_current_session()
                    smtc_listener.current_session = session
                    if session is not None:
                        props = await asyncio.wait_for(
                            session.try_get_media_properties_async(), timeout=2.0
                        )
                        cur = (props.title or "", props.artist or "")
                        if cur != last_seen and (cur[0] or cur[1]):
                            last_seen = cur
                            if smtc_listener._props_token is None:
                                smtc_listener._subscribe(session)
                            await _refresh_current_track(session)
                    else:
                        if last_seen != ("", ""):
                            last_seen = ("", "")
                            await _clear_state()
                except asyncio.TimeoutError:
                    pass  # WinRT завис — пропускаем этот цикл поллинга
                except Exception:  # noqa: BLE001
                    log.debug("[поллинг] ошибка чтения сессии")

            # --- Плавный прогресс ---
            if not manager.has_clients:
                continue
            if media_state.is_playing and media_state.duration > 0:
                media_state.position = min(
                    media_state.duration,
                    media_state.position + PROGRESS_TICK_INTERVAL,
                )
                await manager.broadcast_json(media_state.as_progress())
    except asyncio.CancelledError:
        pass  # штатная остановка при завершении сервера
