"""
Белый список приложений для запуска с телефона.

ЭТО ЕДИНСТВЕННОЕ МЕСТО, которое решает, что можно запускать. Телефон присылает
лишь "id" из этого списка — сервер сам берёт путь отсюда. Поэтому запустить
произвольный exe, подменив запрос с телефона, НЕЛЬЗЯ: чего нет в списке —
того не существует для сервера.

Чтобы добавить приложение — допиши запись в APPS ниже.

Поля записи:
  id     — короткий идентификатор (латиница, без пробелов), уникальный
  name   — как показать на плитке
  icon   — эмодзи или 1-2 буквы для плитки (просто и без файлов)
  target — что запускать. Варианты:
             • полный путь к .exe:      r"C:\\Program Files\\...\\app.exe"
             • имя в PATH:               "notepad"
             • URL (откроется в браузере): "https://ya.ru"
             • shell-команда UWP/протокол: "explorer.exe shell:AppsFolder\\..."
  args   — (необязательно) список аргументов к target
  proc   — (необязательно) имя процесса или список имён для функции «закрыть»,
           если оно отличается от имени файла в target. Пример: лаунчер
           запускает game.exe, а реально работает Game-Win64-Shipping.exe —
           тогда proc="Game-Win64-Shipping.exe". Обычно НЕ нужно: имя берётся
           из target автоматически.

Примеры ниже закомментированы — раскомментируй и поправь пути под свой ПК.
"""
from __future__ import annotations

APPS: list[dict] = [
    # --- Встроенные в Windows (работают сразу, без правки путей) ---
    {"id": "notepad",   "name": "Блокнот",     "icon": "📝", "target": "notepad"},
    # Калькулятор в Win10/11 — UWP: запускается как 'calc', но реальный процесс
    # называется иначе, поэтому для «закрыть» указываем настоящие имена в proc.
    {"id": "calc",      "name": "Калькулятор", "icon": "🧮", "target": "calc",
     "proc": ["CalculatorApp.exe", "Calculator.exe"]},
    {"id": "explorer",  "name": "Проводник",   "icon": "🗂", "target": "explorer"},
    {"id": "taskmgr",   "name": "Диспетчер",   "icon": "📊", "target": "taskmgr",
     "proc": "Taskmgr.exe"},

    # --- Сайты (откроются в браузере по умолчанию) ---
    {"id": "youtube",   "name": "YouTube",     "icon": "▶️", "target": "https://youtube.com"},

    # --- Твои программы: укажи путь к .exe и раскомментируй ---
    # {"id": "steam",   "name": "Steam",       "icon": "🎮",
    #  "target": r"C:\\Program Files (x86)\\Steam\\steam.exe"},
    # {"id": "obs",     "name": "OBS",         "icon": "🎥",
    #  "target": r"C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe"},
    # {"id": "chrome",  "name": "Chrome",      "icon": "🌐",
    #  "target": r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"},
    # {"id": "yamusic", "name": "Я.Музыка",    "icon": "🎵",
    #  "target": r"C:\\Users\\user\\AppData\\Local\\Programs\\YandexMusic\\Яндекс Музыка.exe"},
]
