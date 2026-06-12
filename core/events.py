"""
Helm · события — лог действий и уведомления на ПК.

Лог — кольцевой буфер в памяти (последние N событий): входы гостей, неудачные
и удачные попытки входа в админку, ключевые действия. Админ смотрит его в UI.

Тост на ПК — короткое всплывающее уведомление средствами Windows. Чтобы не
тянуть зависимости, используем PowerShell (BurntToast если есть, иначе
простое уведомление через MessageBox-баллон не делаем — только тихий лог).
Если показать тост не вышло — просто пишем в лог, ничего не падает.
"""
from __future__ import annotations

import logging
import sys
import time
from collections import deque
from threading import Lock

log = logging.getLogger("helm")

_IS_WIN = sys.platform.startswith("win")
_MAX_EVENTS = 200

_events: deque[dict] = deque(maxlen=_MAX_EVENTS)
_lock = Lock()


def add_event(kind: str, text: str, *, who: str | None = None) -> dict:
    """Записать событие в лог. kind — тип (guest_join, admin_ok, admin_fail, action)."""
    ev = {
        "ts": time.time(),
        "kind": kind,
        "text": text,
        "who": who,
    }
    with _lock:
        _events.append(ev)
    log.info("Событие [%s] %s%s", kind, text, f" ({who})" if who else "")
    return ev


def recent(limit: int = 100) -> list[dict]:
    """Последние события (новые сверху)."""
    with _lock:
        items = list(_events)
    items.reverse()
    return items[:limit]


def clear() -> None:
    with _lock:
        _events.clear()


def toast_pc(title: str, message: str) -> None:
    """Показать короткое уведомление на ПК (Windows). Тихо игнорирует ошибки."""
    if not _IS_WIN:
        return
    try:
        import subprocess

        # Экранируем спецсимволы PowerShell, чтобы имя гостя с кавычками/
        # бэктиками не сломало скрипт и не позволило подставить код.
        def _esc(s: str) -> str:
            s = str(s)[:80]
            for ch in ('`', '"', '$', '\r', '\n'):
                s = s.replace(ch, ' ')
            return s

        safe_title = _esc(title)
        safe_message = _esc(message)
        # Нативный тост через PowerShell + WinRT ToastNotification.
        ps = f'''
$ErrorActionPreference='Stop'
try {{
  [Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
  $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $texts = $xml.GetElementsByTagName("text")
  $texts.Item(0).AppendChild($xml.CreateTextNode("{safe_title}")) | Out-Null
  $texts.Item(1).AppendChild($xml.CreateTextNode("{safe_message}")) | Out-Null
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Helm")
  $notifier.Show($toast)
}} catch {{ }}
'''
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            creationflags=0x08000000,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("toast_pc не сработал: %s", e)
