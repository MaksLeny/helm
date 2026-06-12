"""
Helm · монитор — чтение температуры из Core Temp (Shared Memory).

Core Temp публикует данные о ядрах в именованную разделяемую память
"CoreTempMappingObject" по структуре CORE_TEMP_SHARED_DATA из официального SDK.
Это самый точный источник температуры ядер CPU на Windows и не требует ни
драйверов от нас, ни сторонних Python-библиотек — только stdlib ctypes.

Условие: Core Temp должен быть запущен. Иначе open file-mapping не удастся и
вернём None (монитор уйдёт к другим источникам / «н/д»).

Важная тонкость структуры:
  • fTemp[i] — температура ядра i. Если ucDeltaToTjMax == 1, то это НЕ абсолютная
    температура, а дельта до TjMax: реальная = uiTjMax[socket] - fTemp[i].
  • ucFahrenheit == 1 — значения в Фаренгейтах (переводим в Цельсий).
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

log = logging.getLogger("helm")

_IS_WIN = sys.platform.startswith("win")

# Размеры массивов из CoreTempSDK.
_LOAD_N = 256
_TJMAX_N = 128
_TEMP_N = 256
_NAME_N = 100


def _build_struct():
    """Собрать ctypes-структуру CORE_TEMP_SHARED_DATA (только на Windows)."""
    import ctypes

    class CoreTempSharedData(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("uiLoad", ctypes.c_uint * _LOAD_N),
            ("uiTjMax", ctypes.c_uint * _TJMAX_N),
            ("uiCoreCnt", ctypes.c_uint),
            ("uiCPUCnt", ctypes.c_uint),
            ("fTemp", ctypes.c_float * _TEMP_N),
            ("fVID", ctypes.c_float),
            ("fCPUSpeed", ctypes.c_float),
            ("fFSBSpeed", ctypes.c_float),
            ("fMultiplier", ctypes.c_float),
            ("sCPUName", ctypes.c_char * _NAME_N),
            ("ucFahrenheit", ctypes.c_ubyte),
            ("ucDeltaToTjMax", ctypes.c_ubyte),
        ]

    return CoreTempSharedData


def read_coretemp() -> Optional[float]:
    """Максимальная температура ядра из Core Temp, либо None."""
    if not _IS_WIN:  # pragma: no cover - только Windows
        return None
    try:
        import ctypes
        from ctypes import wintypes

        FILE_MAP_READ = 0x0004
        kernel32 = ctypes.windll.kernel32

        OpenFileMappingW = kernel32.OpenFileMappingW
        OpenFileMappingW.restype = wintypes.HANDLE
        OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]

        MapViewOfFile = kernel32.MapViewOfFile
        MapViewOfFile.restype = wintypes.LPVOID
        MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]

        UnmapViewOfFile = kernel32.UnmapViewOfFile
        UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]

        Struct = _build_struct()

        handle = OpenFileMappingW(FILE_MAP_READ, False, "CoreTempMappingObject")
        if not handle:
            return None  # Core Temp не запущен
        ptr = None
        try:
            ptr = MapViewOfFile(handle, FILE_MAP_READ, 0, 0, ctypes.sizeof(Struct))
            if not ptr:
                return None
            data = ctypes.cast(ptr, ctypes.POINTER(Struct)).contents

            core_cnt = int(data.uiCoreCnt)
            cpu_cnt = max(1, int(data.uiCPUCnt))
            if core_cnt <= 0 or core_cnt > _TEMP_N:
                return None

            is_f = bool(data.ucFahrenheit)
            is_delta = bool(data.ucDeltaToTjMax)

            best: Optional[float] = None
            total_cores = core_cnt * cpu_cnt
            total_cores = min(total_cores, _TEMP_N)
            for i in range(total_cores):
                raw = float(data.fTemp[i])
                socket = 0  # для бытовых одно-сокетных систем достаточно индекса 0
                if is_delta:
                    tjmax = float(data.uiTjMax[socket]) if data.uiTjMax[socket] else 100.0
                    temp_c = tjmax - raw
                else:
                    temp_c = raw
                if is_f:
                    temp_c = (temp_c - 32.0) * 5.0 / 9.0
                if 0.0 < temp_c < 130.0:
                    best = temp_c if best is None else max(best, temp_c)
            return round(best, 1) if best is not None else None
        finally:
            if ptr:
                UnmapViewOfFile(ptr)
            CloseHandle(handle)
    except Exception as e:  # noqa: BLE001
        log.debug("Core Temp Shared Memory недоступна: %s", e)
        return None
