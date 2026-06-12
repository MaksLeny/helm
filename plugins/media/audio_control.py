"""
Управление звуком на ПК через pycaw:
  - VolumeController        — общая системная громкость / mute
  - AppVolumeController     — поканальная громкость (по приложениям)
Плюс эмуляция медиаклавиш Windows (play/pause/next/prev) через keybd_event.
"""
from __future__ import annotations

import ctypes
import logging
from typing import Optional

from .media_config import (
    KEYEVENTF_KEYDOWN,
    KEYEVENTF_KEYUP,
)

log = logging.getLogger("winwave")

# pycaw — управление громкостью (опционально). Если не установлен —
# сервер работает, но слайдеры громкости будут неактивны.
try:
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, ISimpleAudioVolume
    _PYCAW_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PYCAW_AVAILABLE = False

def _press_media_key(vk_code: int) -> None:
    """Эмулировать нажатие и отпускание медиаклавиши через WinAPI keybd_event."""
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYDOWN, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
        log.debug("Эмулирована медиаклавиша 0x%02X", vk_code)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось эмулировать медиаклавишу 0x%02X", vk_code)


class VolumeController:
    """
    Системная громкость через COM-интерфейс IAudioEndpointVolume.

    COM апартаментно-потоковый: интерфейс, созданный в одном потоке, нельзя
    надёжно использовать из другого. uvicorn вызывает get/set из своих потоков,
    поэтому мы инициализируем COM и получаем интерфейс внутри КАЖДОГО вызова.
    Это чуть дороже, но работает стабильно и не молчит при сбое.
    """

    def __init__(self) -> None:
        self._ok = False
        self._cached_level: Optional[int] = None
        self._cached_at: float = 0.0
        if _PYCAW_AVAILABLE:
            # Разовая проверка доступности (в основном потоке)
            try:
                import comtypes
                comtypes.CoInitialize()
                try:
                    device = AudioUtilities.GetSpeakers()
                    _ = device.EndpointVolume
                    self._ok = True
                finally:
                    comtypes.CoUninitialize()
            except Exception:  # noqa: BLE001
                log.exception("Управление громкостью недоступно")
                self._ok = False

    @property
    def available(self) -> bool:
        return self._ok

    def _with_volume(self, fn):
        """Выполнить fn(volume_interface) в правильно инициализированном COM."""
        if not self._ok:
            return None
        import comtypes
        initialized = False
        try:
            comtypes.CoInitialize()
            initialized = True
        except Exception:  # noqa: BLE001
            # Уже инициализирован в этом потоке — это нормально
            pass
        try:
            device = AudioUtilities.GetSpeakers()
            volume = device.EndpointVolume
            return fn(volume)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка операции с громкостью")
            return None
        finally:
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    def get_level(self) -> Optional[int]:
        """Текущая громкость 0..100, либо None. Кэшируется на 0.5с."""
        import time
        now = time.monotonic()
        if (self._cached_level is not None
                and now - self._cached_at < 0.5):
            return self._cached_level

        def _read(volume):
            scalar = volume.GetMasterVolumeLevelScalar()
            return int(round(scalar * 100))
        level = self._with_volume(_read)
        if level is not None:
            self._cached_level = level
            self._cached_at = now
        return level

    def set_level(self, percent: int) -> None:
        """Установить громкость 0..100."""
        import time
        # Сразу обновляем кэш, чтобы UI не дёргался
        self._cached_level = max(0, min(100, percent))
        self._cached_at = time.monotonic()

        def _write(volume):
            scalar = max(0.0, min(1.0, percent / 100.0))
            volume.SetMasterVolumeLevelScalar(scalar, None)
            return True
        self._with_volume(_write)

    def get_muted(self) -> Optional[bool]:
        """Состояние «без звука»: True/False, либо None если недоступно."""
        def _read(volume):
            return bool(volume.GetMute())
        return self._with_volume(_read)

    def toggle_mute(self) -> Optional[bool]:
        """Переключить mute. Возвращает новое состояние (True=без звука)."""
        def _toggle(volume):
            new_state = 0 if volume.GetMute() else 1
            volume.SetMute(new_state, None)
            return bool(new_state)
        return self._with_volume(_toggle)


volume_controller = VolumeController()


class AppVolumeController:
    """
    Поканальная громкость: отдельный уровень/mute для каждого приложения
    через сессии аудио-микшера Windows (ISimpleAudioVolume).

    Соблюдаем тот же COM-апартаментный принцип, что и VolumeController:
    инициализируем COM и перечисляем сессии внутри КАЖДОГО вызова, потому что
    uvicorn зовёт нас из разных потоков.

    Сессии группируем по имени процесса (Discord.exe, Spotify.exe, …): у одного
    приложения может быть несколько сессий, и PID меняется — имя стабильнее.
    """

    def __init__(self) -> None:
        self._ok = _PYCAW_AVAILABLE and volume_controller.available
        self._cache: list[dict] = []
        self._cached_at: float = 0.0

    @property
    def available(self) -> bool:
        return self._ok

    def _enum(self, fn):
        """Выполнить fn(list_of_sessions) в правильно инициализированном COM."""
        if not self._ok:
            return None
        import comtypes
        initialized = False
        try:
            comtypes.CoInitialize()
            initialized = True
        except Exception:  # noqa: BLE001
            pass
        try:
            sessions = AudioUtilities.GetAllSessions()
            return fn(sessions)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка перечисления аудио-сессий")
            return None
        finally:
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _proc_name(session) -> Optional[str]:
        try:
            if session.Process and session.Process.name():
                return session.Process.name()
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _vol_iface(session):
        """Получить ISimpleAudioVolume у сессии (с запасным путём)."""
        try:
            iface = session.SimpleAudioVolume
            if iface is not None:
                return iface
        except Exception:  # noqa: BLE001
            pass
        # Запасной путь для некоторых сборок pycaw
        try:
            return session._ctl.QueryInterface(ISimpleAudioVolume)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _friendly(name: str) -> str:
        """Discord.exe -> Discord; делаем человекочитаемое имя."""
        base = name[:-4] if name.lower().endswith(".exe") else name
        return base

    def list_apps(self) -> list[dict]:
        """
        Активные приложения со звуком. Кэш на 1с, чтобы не дёргать COM часто.
        [{key, name, volume, muted}] — key это имя процесса (нижний регистр).
        """
        import time
        now = time.monotonic()
        if self._cache and now - self._cached_at < 1.0:
            return self._cache

        def _list(sessions):
            agg: dict[str, dict] = {}
            for s in sessions:
                pname = self._proc_name(s)
                if not pname:
                    continue   # системные звуки без процесса — пропускаем
                vol = self._vol_iface(s)
                if vol is None:
                    continue
                key = pname.lower()
                try:
                    level = int(round(vol.GetMasterVolume() * 100))
                    muted = bool(vol.GetMute())
                except Exception:  # noqa: BLE001
                    continue
                # Если у приложения несколько сессий — берём первую как репрезентативную
                if key not in agg:
                    agg[key] = {
                        "key": key,
                        "name": self._friendly(pname),
                        "volume": level,
                        "muted": muted,
                    }
            # Сортируем по имени для стабильного порядка в UI
            return sorted(agg.values(), key=lambda a: a["name"].lower())

        result = self._enum(_list)
        if result is not None:
            self._cache = result
            self._cached_at = now
            return result
        return self._cache

    def set_app_volume(self, key: str, percent: int) -> None:
        """Громкость 0..100 для всех сессий приложения с этим именем процесса."""
        key = (key or "").lower()
        scalar = max(0.0, min(1.0, percent / 100.0))

        def _apply(sessions):
            hit = False
            for s in sessions:
                pname = self._proc_name(s)
                if pname and pname.lower() == key:
                    vol = self._vol_iface(s)
                    if vol is None:
                        continue
                    try:
                        vol.SetMasterVolume(scalar, None)
                        hit = True
                    except Exception:  # noqa: BLE001
                        pass
            return hit
        self._enum(_apply)
        # Сбрасываем кэш, чтобы следующий list_apps отдал свежие значения
        self._cached_at = 0.0

    def toggle_app_mute(self, key: str) -> Optional[bool]:
        """Переключить mute приложения. Возвращает новое состояние или None."""
        key = (key or "").lower()

        def _toggle(sessions):
            new_state = None
            for s in sessions:
                pname = self._proc_name(s)
                if pname and pname.lower() == key:
                    vol = self._vol_iface(s)
                    if vol is None:
                        continue
                    try:
                        cur = vol.GetMute()
                        new_state = 0 if cur else 1
                        vol.SetMute(new_state, None)
                    except Exception:  # noqa: BLE001
                        pass
            return None if new_state is None else bool(new_state)
        res = self._enum(_toggle)
        self._cached_at = 0.0
        return res


app_volume_controller = AppVolumeController()
