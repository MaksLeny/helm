"""
Helm · монитор — температуры из внешних источников.

psutil на Windows почти никогда не отдаёт температуру CPU (нет стандартного
API). Поэтому пробуем по очереди:

  1. LibreHardwareMonitor / OpenHardwareMonitor — если запущены с включённым
     встроенным веб-сервером, они отдают JSON по http://localhost:8085/data.json
     (порт настраивается). Это самый точный и простой путь — никаких драйверов
     со стороны Helm. Пользователь просто держит LHM запущенным.
  2. WMI MSAcpi_ThermalZoneTemperature — грубая «температура зоны» (часто есть
     даже без сторонних программ, но менее точна, иногда в Кельвинах*10).

Любой сбой → None, монитор покажет «н/д». Запросы короткие и не блокируют надолго.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import urllib.request
from typing import Optional

log = logging.getLogger("helm")

_IS_WIN = sys.platform.startswith("win")
_NO_WINDOW = 0x08000000 if _IS_WIN else 0

# Где искать веб-сервер LHM/OHM (можно переопределить из конфига монитора).
LHM_URL = "http://localhost:8085/data.json"
_HTTP_TIMEOUT = 0.8


def _walk_lhm(node: dict, out: list[float]) -> None:
    """Рекурсивно собрать все значения температуры из дерева LHM/OHM JSON."""
    try:
        text = str(node.get("Text", ""))
        value = node.get("Value", "")
        # Узлы температур имеют единицу "°C" в Value и тип Temperature.
        if isinstance(value, str) and "°C" in value:
            m = re.search(r"(-?\d+[.,]?\d*)", value)
            if m:
                out.append(float(m.group(1).replace(",", ".")))
        elif str(node.get("Type", "")).lower() == "temperature":
            m = re.search(r"(-?\d+[.,]?\d*)", str(value))
            if m:
                out.append(float(m.group(1).replace(",", ".")))
        # ignore unused text
        _ = text
    except Exception:  # noqa: BLE001
        pass
    for child in node.get("Children", []) or []:
        _walk_lhm(child, out)


def _from_lhm(url: str = LHM_URL) -> Optional[float]:
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    temps: list[float] = []
    _walk_lhm(data, temps)
    # Берём максимум разумных значений (CPU/GPU обычно 30..100).
    sane = [t for t in temps if 10.0 < t < 120.0]
    if not sane:
        return None
    return round(max(sane), 1)


def _from_wmi() -> Optional[float]:
    """Грубая температура через WMI (PowerShell). Часто в децикельвинах."""
    if not _IS_WIN:  # pragma: no cover
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance -Namespace root/wmi "
             "-ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop)"
             ".CurrentTemperature"],
            capture_output=True, timeout=3.0, creationflags=_NO_WINDOW,
        )
        if out.returncode != 0:
            return None
        raw = (out.stdout or b"").decode("utf-8", errors="replace").strip()
        vals = [int(x) for x in re.findall(r"\d+", raw)]
        if not vals:
            return None
        # MSAcpi отдаёт десятые доли Кельвина: C = v/10 - 273.15
        c = vals[0] / 10.0 - 273.15
        if 10.0 < c < 120.0:
            return round(c, 1)
        return None
    except Exception:  # noqa: BLE001
        return None


def external_temp(lhm_url: str = LHM_URL) -> Optional[float]:
    """Температура из внешних источников по приоритету точности; иначе None.

    Порядок: Core Temp (Shared Memory, температура ЯДЕР — самый точный) →
    LibreHardwareMonitor/OpenHardwareMonitor (HTTP JSON) → WMI (грубая зона,
    наименее точная, может занижать — поэтому последняя).
    """
    val, _src = external_temp_src(lhm_url)
    return val


def external_temp_src(lhm_url: str = LHM_URL):
    """Как external_temp, но возвращает (значение|None, источник|None).

    Источник: 'Core Temp' | 'LHM' | 'WMI' | None — для отображения в UI.
    """
    from .coretemp import read_coretemp
    t = read_coretemp()
    if t is not None:
        return t, "Core Temp"
    t = _from_lhm(lhm_url)
    if t is not None:
        return t, "LHM"
    t = _from_wmi()
    if t is not None:
        return t, "WMI"
    return None, None
