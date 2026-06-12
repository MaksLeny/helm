"""
Helm · буфер обмена — история записей (в памяти сервера).

Хранит последние записи, которыми обменивались телефон и ПК. Живёт в RAM,
сбрасывается при перезапуске сервера (как история медиа). Без БД и файлов.

Запись (dict):
  id:   str   — уникальный идентификатор (для адресных команд copy/delete)
  text: str   — содержимое
  ts:   float — время добавления (unix)
  kind: str   — 'url' если текст похож на ссылку, иначе 'text'
  src:  str   — 'phone' (пришло с телефона) | 'pc' (вытянуто с ПК)
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger("helm")

_MAX = 30                 # сколько записей помним
_MAX_LEN = 20000          # обрезаем слишком длинные тексты (защита от мусора)

# Файл постоянной истории рядом с проектом (переживает перезагрузку ПК).
_HISTORY_FILE = Path(__file__).parent.parent.parent / "helm_clipboard_history.json"

# Грубое распознавание ссылки: http(s):// или www. или схема://
_URL_RE = re.compile(r"^\s*(https?://|www\.|[a-z][a-z0-9+.\-]*://)", re.IGNORECASE)


def _kind(text: str) -> str:
    return "url" if _URL_RE.match(text or "") else "text"


class ClipStore:
    """Кольцевая история буфера обмена с защитой от дублей подряд.

    История сохраняется на диск (JSON) и подгружается при старте — буфер
    переживает перезагрузку ПК. Сами файлы/картинки лежат в постоянной папке
    blobs; при загрузке проверяем, что blob ещё на месте.
    """

    def __init__(self) -> None:
        self._items: deque[dict] = deque(maxlen=_MAX)
        self._seq = 0

    def load(self) -> None:
        """Загрузить историю с диска (при старте плагина)."""
        from . import blobs
        try:
            if not _HISTORY_FILE.exists():
                return
            data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            items = data.get("items", [])
            seq = int(data.get("seq", 0))
            restored: list[dict] = []
            for it in items:
                # Для блобов проверяем, что файл ещё на месте — иначе пропускаем.
                if it.get("kind") in ("image", "file"):
                    if not blobs.blob_path(it.get("blob_id", "")):
                        continue
                restored.append(it)
            # deque заполняем в обратном порядке (appendleft хранит новейшие слева)
            self._items.clear()
            for it in reversed(restored):
                self._items.appendleft(it)
            self._seq = seq
            log.info("Буфер: загружено записей из памяти: %d", len(self._items))
        except Exception as e:  # noqa: BLE001
            log.warning("Буфер: не удалось загрузить историю: %s", e)

    def _save(self) -> None:
        """Сохранить историю на диск (после каждого изменения)."""
        try:
            data = {"seq": self._seq, "items": list(self._items)}
            _HISTORY_FILE.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("Буфер: не удалось сохранить историю: %s", e)

    def add(self, text: str, src: str = "phone") -> Optional[dict]:
        """Добавить запись. Возвращает её dict или None (если пусто/дубль)."""
        if text is None:
            return None
        text = text[:_MAX_LEN]
        if not text.strip():
            return None
        # Не плодим дубли, если самый свежий элемент совпадает.
        if self._items and self._items[0].get("text") == text:
            self._items[0]["ts"] = time.time()
            self._save()
            return self._items[0]
        self._seq += 1
        item = {
            "id": f"c{self._seq}",
            "text": text,
            "ts": time.time(),
            "kind": _kind(text),
            "src": src,
        }
        self._items.appendleft(item)
        self._save()
        return item

    def get(self, item_id: str) -> Optional[dict]:
        for it in self._items:
            if it["id"] == item_id:
                return it
        return None

    def add_blob(self, *, kind: str, blob_id: str, filename: str,
                 size: int, mime: str = "", src: str = "phone") -> dict:
        """Добавить запись о картинке/файле (kind='image'|'file')."""
        self._seq += 1
        item = {
            "id": f"c{self._seq}",
            "text": filename,            # отображаемое имя
            "ts": time.time(),
            "kind": kind,                # 'image' | 'file'
            "src": src,
            "blob_id": blob_id,
            "filename": filename,
            "size": int(size),
            "mime": mime,
        }
        self._items.appendleft(item)
        self._save()
        return item

    def delete(self, item_id: str) -> bool:
        from . import blobs
        for it in list(self._items):
            if it["id"] == item_id:
                # если у записи есть файл — удалим и его с диска
                if it.get("blob_id"):
                    blobs.delete_blob(it["blob_id"])
                self._items.remove(it)
                self._save()
                return True
        return False

    def clear(self) -> int:
        from . import blobs
        n = len(self._items)
        self._items.clear()
        self._save()
        blobs.cleanup_all()        # стираем и сами файлы
        return n

    def list(self) -> list[dict]:
        """Все записи, новейшие первыми."""
        return list(self._items)
