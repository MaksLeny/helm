"""
Helm · монитор — настройки раздела.

LHM_URL — адрес встроенного веб-сервера LibreHardwareMonitor или
OpenHardwareMonitor для чтения температур. Чтобы заработало:
  • LibreHardwareMonitor: Options → "Remote Web Server" → Run (порт по умолч. 8085);
  • OpenHardwareMonitor: Options → "Remote Web Server" → Run.
Если программа не запущена — монитор молча использует другие источники (WMI)
или покажет «н/д». Менять порт здесь, если он отличается от 8085.
"""
from __future__ import annotations

import os

LHM_URL: str = os.environ.get("HELM_LHM_URL", "http://localhost:8085/data.json")
