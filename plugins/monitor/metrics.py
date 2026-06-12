"""
Сбор метрик системы через psutil.

Скоростные показатели (диск, сеть) считаются как дельта между замерами,
поэтому коллектор хранит предыдущие значения и момент времени. Всё, чего нет
на конкретном железе (температуры, батарея), отдаётся как None — UI покажет
«н/д» вместо падения.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .temps import external_temp_src

try:
    import psutil
    _PSUTIL = True
except Exception:  # noqa: BLE001
    _PSUTIL = False

log = logging.getLogger("helm")


class MetricsCollector:
    def __init__(self) -> None:
        self.available = _PSUTIL
        self._prev_disk = None     # (read_bytes, write_bytes, t)
        self._prev_net = None      # (sent, recv, t)
        self._boot = psutil.boot_time() if _PSUTIL else 0.0
        self._proc_seen: set[int] = set()
        # Кэш внешней температуры (LHM/WMI): дёргаем не чаще раза в N секунд,
        # чтобы HTTP-запрос не выполнялся на каждом тике (1с).
        self._ext_temp: Optional[float] = None
        self._ext_temp_at: float = 0.0
        self._ext_temp_period: float = 5.0
        self._ext_temp_src: Optional[str] = None
        self._temp_src: Optional[str] = None
        self.lhm_url: Optional[str] = None  # переопределяется плагином из конфига
        # Первый вызов cpu_percent «заводит» счётчик, чтобы дальше не было 0.0
        if _PSUTIL:
            try:
                psutil.cpu_percent(interval=None)
                psutil.cpu_percent(interval=None, percpu=True)
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- helpers
    def _temps(self) -> Optional[float]:
        """Температура. Приоритет точности: внешний источник (Core Temp →
        LHM → WMI, с кэшем), и лишь если он молчит — psutil-датчики.

        Core Temp/LHM дают температуру ЯДЕР CPU; psutil на Windows обычно пуст
        или отдаёт грубую «зону», поэтому внешний источник идёт первым.
        Источник сохраняется в self._temp_src для отображения/диагностики.
        """
        # Внешний источник с кэшем (не чаще раза в _ext_temp_period сек).
        now = time.monotonic()
        if now - self._ext_temp_at >= self._ext_temp_period:
            self._ext_temp_at = now
            try:
                url = self.lhm_url
                val, src = external_temp_src(url) if url else external_temp_src()
                self._ext_temp = val
                self._ext_temp_src = src
            except Exception:  # noqa: BLE001
                self._ext_temp = None
                self._ext_temp_src = None
        if self._ext_temp is not None:
            self._temp_src = self._ext_temp_src
            return self._ext_temp
        # Фоллбэк: psutil-датчики (на Linux часто работают).
        try:
            data = psutil.sensors_temperatures()
        except Exception:  # noqa: BLE001
            data = None
        if data:
            best = None
            for entries in data.values():
                for e in entries:
                    if e.current and e.current > 0:
                        best = e.current if best is None else max(best, e.current)
            if best is not None:
                self._temp_src = "psutil"
                return round(best, 1)
        self._temp_src = None
        return None

    @staticmethod
    def _battery() -> Optional[dict]:
        try:
            b = psutil.sensors_battery()
        except Exception:  # noqa: BLE001
            return None
        if b is None:
            return None
        secs = b.secsleft
        if secs is not None and secs < 0:
            secs = None    # неизвестно / заряжается
        return {
            "percent": round(b.percent),
            "plugged": bool(b.power_plugged),
            "secsleft": secs,
        }

    def _top_processes(self, n: int = 20) -> list[dict]:
        """Топ процессов по CPU (с именем и памятью).

        Тонкость psutil: per-process cpu_percent при первом обращении к
        процессу возвращает 0.0 — счётчик «заводится» между двумя замерами.
        Поэтому держим набор уже опрошенных pid: для них значение реальное,
        для новых — заведём счётчик сейчас, а корректное число придёт на
        следующем тике. Так список перестаёт быть «нулевым».
        """
        procs = []
        seen = getattr(self, "_proc_seen", None)
        if seen is None:
            seen = self._proc_seen = set()
        ema = getattr(self, "_cpu_ema", None)
        if ema is None:
            ema = self._cpu_ema = {}
        alive: set[int] = set()
        ncpu = psutil.cpu_count() or 1
        try:
            for p in psutil.process_iter(["pid", "name", "memory_percent"]):
                pid = p.info.get("pid")
                alive.add(pid)
                try:
                    cpu = p.cpu_percent(interval=None) / ncpu  # нормируем на ядра
                except Exception:  # noqa: BLE001
                    cpu = 0.0
                if pid not in seen:
                    # первый раз видим — счётчик только что заведён, пропустим
                    seen.add(pid)
                    ema[pid] = cpu
                    continue
                # EMA-сглаживание: убирает резкие скачки процентов, из-за
                # которых строки прыгали в сортировке. alpha=0.4 — заметная
                # реакция, но без дёрганья.
                prev = ema.get(pid, cpu)
                sm = prev * 0.6 + cpu * 0.4
                ema[pid] = sm
                procs.append({
                    "pid": pid,
                    "name": p.info.get("name") or "?",
                    "cpu": round(sm, 1),
                    "mem": round(p.info.get("memory_percent") or 0.0, 1),
                })
        except Exception:  # noqa: BLE001
            return []
        # Забываем умершие процессы, чтобы словари не росли.
        self._proc_seen = seen & alive
        self._cpu_ema = {pid: v for pid, v in ema.items() if pid in alive}
        # Сортировка по сглаженному CPU, затем память, затем pid (стабильные
        # вторичные ключи). Перестановку строк фронт делает не чаще раза в 3с.
        procs.sort(key=lambda x: (-x["cpu"], -x["mem"], x["pid"]))
        return procs[:n]

    # ------------------------------------------------------------------ public
    def snapshot(self) -> dict:
        """Собрать текущий срез метрик в готовый к отправке словарь."""
        if not _PSUTIL:
            return {"type": "metrics", "available": False}

        now = time.monotonic()

        # CPU
        cpu_total = psutil.cpu_percent(interval=None)
        try:
            cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
        except Exception:  # noqa: BLE001
            cpu_cores = []
        try:
            freq = psutil.cpu_freq()
            cpu_mhz = round(freq.current) if freq else None
        except Exception:  # noqa: BLE001
            cpu_mhz = None

        # Память
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()

        # Диски (по разделам)
        disks = []
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    disks.append({
                        "mount": part.mountpoint,
                        "percent": u.percent,
                        "used": u.used,
                        "total": u.total,
                    })
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

        # Скорость диска (дельта)
        disk_rate = {"read": 0.0, "write": 0.0}
        try:
            dio = psutil.disk_io_counters()
            if dio and self._prev_disk:
                pr, pw, pt = self._prev_disk
                dt = max(1e-3, now - pt)
                disk_rate = {
                    "read": max(0.0, (dio.read_bytes - pr) / dt),
                    "write": max(0.0, (dio.write_bytes - pw) / dt),
                }
            if dio:
                self._prev_disk = (dio.read_bytes, dio.write_bytes, now)
        except Exception:  # noqa: BLE001
            pass

        # Скорость сети (дельта)
        net_rate = {"up": 0.0, "down": 0.0}
        try:
            nio = psutil.net_io_counters()
            if nio and self._prev_net:
                ps, prv, pt = self._prev_net
                dt = max(1e-3, now - pt)
                net_rate = {
                    "up": max(0.0, (nio.bytes_sent - ps) / dt),
                    "down": max(0.0, (nio.bytes_recv - prv) / dt),
                }
            if nio:
                self._prev_net = (nio.bytes_sent, nio.bytes_recv, now)
        except Exception:  # noqa: BLE001
            pass

        uptime = int(time.time() - self._boot) if self._boot else None

        return {
            "type": "metrics",
            "available": True,
            "cpu": {"total": round(cpu_total, 1), "cores": cpu_cores, "mhz": cpu_mhz},
            "mem": {"percent": vm.percent, "used": vm.used, "total": vm.total},
            "swap": {"percent": sm.percent, "used": sm.used, "total": sm.total},
            "disks": disks,
            "disk_rate": disk_rate,
            "net_rate": net_rate,
            "temp": self._temps(),
            "temp_src": self._temp_src,
            "battery": self._battery(),
            "uptime": uptime,
            "top": self._top_processes(),
        }

    # ------------------------------------------------------------------ kill
    def kill_process(self, pid: int) -> tuple[bool, str]:
        """Завершить процесс по PID. Возвращает (ok, имя_или_ошибка)."""
        if not _PSUTIL:
            return False, "psutil недоступен"
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False, "некорректный pid"
        # Не даём убивать системные критичные pid (0/4 на Windows, 1 на *nix).
        if pid in (0, 1, 4):
            return False, "системный процесс защищён"
        try:
            p = psutil.Process(pid)
            name = p.name()
            p.terminate()
            try:
                p.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                # не завершился по-хорошему — принудительно
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            return True, name
        except psutil.NoSuchProcess:
            return False, "процесс уже завершён"
        except psutil.AccessDenied:
            return False, "нет прав (нужен запуск от админа)"
        except Exception as e:  # noqa: BLE001
            return False, str(e)
