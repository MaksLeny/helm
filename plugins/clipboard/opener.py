"""
Helm · буфер обмена — открыть ссылку в браузере ПК.

Безопасность: открываем только http/https-ссылки. Любые другие схемы
(file:, javascript:, прочее) отклоняем — чтобы с телефона нельзя было
заставить ПК открыть произвольный локальный путь или служебный протокол.
"""
from __future__ import annotations

import logging
import webbrowser
from typing import Tuple
from urllib.parse import urlparse

log = logging.getLogger("helm")

_ALLOWED_SCHEMES = {"http", "https"}


def open_url(text: str) -> Tuple[bool, str]:
    """Открыть http/https-ссылку в браузере ПК по умолчанию."""
    url = (text or "").strip()
    if not url:
        return False, "пустая ссылка"
    # Допускаем "www.example.com" без схемы — достраиваем http.
    if url.lower().startswith("www."):
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, "разрешены только http/https"
    if not parsed.netloc:
        return False, "некорректная ссылка"
    try:
        ok = webbrowser.open(url)
        return (True, "") if ok else (False, "браузер не открылся")
    except Exception as e:  # noqa: BLE001
        log.warning("open_url не удалось: %s", e)
        return False, str(e)
