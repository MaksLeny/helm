"""
Плагин «Монитор системы».

Раз в секунду собирает метрики (CPU/RAM/диски/сеть/температура/батарея) и
рассылает их через хаб Helm всем подключённым телефонам. Экран — лёгкий
фрагмент со «живыми» показателями и мини-графиком, без iframe (свой CSS
заскоуплен под #plugin-monitor, чтобы не влиять на оболочку).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.plugin_base import Plugin
from core.hub import hub

from .metrics import MetricsCollector
from .monitor_config import LHM_URL

log = logging.getLogger("helm")

_SCREEN_HTML: str = (Path(__file__).parent / "screen.html").read_text(encoding="utf-8")

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="18" height="12" rx="2"/>'
    '<path d="M8 20h8M12 16v4"/><path d="M6 10l2.5-3 2.5 4 2.5-5 2.5 4"/></svg>'
)

# Период обновления метрик, секунды.
TICK = 1.0


class MonitorPlugin(Plugin):
    id = "monitor"
    name = "Монитор"
    icon_svg = _ICON

    def __init__(self) -> None:
        super().__init__()
        self._collector = MetricsCollector()
        self._collector.lhm_url = LHM_URL
        self._task: asyncio.Task | None = None

    def frontend(self) -> str:
        return _SCREEN_HTML

    async def handle_ws(self, msg: dict, send) -> None:
        cmd = str(msg.get("cmd", "")).lower()
        if cmd in ("sync", "get"):
            # Мгновенный снимок при открытии раздела — не ждём следующий тик.
            await send(await self._snapshot_async())
            return
        if cmd == "kill":
            pid = msg.get("pid")
            ok, info = self._collector.kill_process(pid)
            log.info("Монитор: kill %s -> %s", pid, "ok:" + info if ok else info)
            await send({"type": "kill_result", "ok": ok, "pid": pid,
                        "message": info})
            # Сразу обновим всем свежий снимок (процесс пропал из топа).
            if ok:
                await hub.broadcast("monitor", await self._snapshot_async())

    async def _snapshot_async(self) -> dict:
        """Снять метрики в отдельном потоке — psutil синхронный и тяжёлый,
        не блокируем event loop (иначе подлагивают другие плагины)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._collector.snapshot)

    async def start(self) -> None:
        if not self._collector.available:
            log.warning("Монитор: psutil недоступен (pip install psutil)")
            return
        # Прогрев: первый снимок «заводит» per-process cpu-счётчики (иначе
        # первый sync вернул бы пустой топ). Делаем в потоке, не блокируя loop.
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._collector.snapshot)
        except Exception:  # noqa: BLE001
            pass
        self._task = asyncio.create_task(self._ticker())
        log.info("Монитор запущен (тик %.1fс)", TICK)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _ticker(self) -> None:
        """Пушим метрики, только когда есть кому слушать."""
        while True:
            try:
                await asyncio.sleep(TICK)
                if hub.connections:
                    snap = await self._snapshot_async()
                    await hub.broadcast("monitor", snap)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в тикере монитора")
