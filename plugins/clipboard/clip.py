"""
Helm · буфер обмена — доступ к системному буферу ОС.

Кроссплатформенно, без сторонних зависимостей (как power/apps):
  • Windows — запись через `clip.exe`, чтение через PowerShell `Get-Clipboard`.
  • Linux   — xclip / xsel / wl-clipboard, если есть (для отладки).
  • macOS   — pbcopy / pbpaste.

Любая операция возвращает (ok: bool, payload: str). Для записи payload —
пустая строка либо текст ошибки; для чтения payload — содержимое буфера либо
текст ошибки. Сетевой ввод никогда не исполняется как команда: текст всегда
передаётся в stdin процесса, а не в аргументы шелла.
"""
from __future__ import annotations

import base64
import logging
import platform
import re as _re
import shutil
import subprocess
from typing import Optional, Tuple

log = logging.getLogger("helm")

_SYS = platform.system()  # 'Windows' | 'Linux' | 'Darwin'

# Скрытое окно для дочерних процессов на Windows (без вспышки консоли).
_NO_WINDOW = 0
if _SYS == "Windows":  # pragma: no cover - зависит от ОС
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _run(args: list[str], *, send_text: Optional[str] = None,
         capture: bool = False, timeout: float = 5.0) -> Tuple[bool, str]:
    """Запустить процесс. Текст (если есть) уходит в stdin, не в аргументы."""
    try:
        proc = subprocess.run(
            args,
            input=(send_text.encode("utf-8") if send_text is not None else None),
            capture_output=capture,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return False, f"нет утилиты: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, "таймаут операции с буфером"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if proc.returncode != 0:
        return False, f"код возврата {proc.returncode}"
    if capture:
        raw = proc.stdout or b""
        try:
            return True, raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return True, raw.decode("cp1251", errors="replace")
    return True, ""


def _run_bytes(args: list[str], data: bytes, timeout: float = 5.0) -> Tuple[bool, str]:
    """Запустить процесс, передав в stdin сырые байты (для clip.exe / UTF-16LE)."""
    try:
        proc = subprocess.run(
            args, input=data, capture_output=False,
            timeout=timeout, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return False, f"нет утилиты: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, "таймаут операции с буфером"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    return (True, "") if proc.returncode == 0 else (False, f"код возврата {proc.returncode}")


# --- Запись в системный буфер -------------------------------------------------

def set_clipboard(text: str) -> Tuple[bool, str]:
    """Положить текст в системный буфер обмена ОС."""
    if text is None:
        text = ""
    if _SYS == "Windows":  # pragma: no cover
        # Кодировку консоли PowerShell нельзя считать UTF-8 (на русской Windows
        # это обычно cp866/cp1251), поэтому НЕ передаём текст напрямую — иначе
        # кириллица бьётся. Шлём base64 от UTF-8 (чистый ASCII, не зависит от
        # кодовой страницы), а PowerShell декодирует обратно в UTF-8 и кладёт
        # в буфер. base64 уходит в stdin, не в аргументы (нет лимита/инъекций).
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            script = (
                "$b64=[Console]::In.ReadToEnd();"
                "$bytes=[Convert]::FromBase64String($b64);"
                "$s=[System.Text.Encoding]::UTF8.GetString($bytes);"
                "Set-Clipboard -Value $s"
            )
            ok, msg = _run(
                [ps, "-NoProfile", "-NonInteractive", "-Command", script],
                send_text=b64,
            )
            if ok:
                return True, ""
            log.warning("Set-Clipboard не сработал: %s, пробую clip.exe", msg)
        # Фоллбэк clip.exe: кодируем текст в UTF-16LE — это «родная» кодировка
        # буфера Windows, clip.exe её принимает корректно для кириллицы.
        try:
            return _run_bytes(["clip"], text.encode("utf-16-le"))
        except Exception as e:  # noqa: BLE001
            return False, str(e)
    if _SYS == "Darwin":  # pragma: no cover
        return _run(["pbcopy"], send_text=text)
    # Linux / прочее
    for tool, args in (
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
    ):
        if shutil.which(tool):
            return _run(args, send_text=text)
    return False, "буфер недоступен (нет xclip/xsel/wl-copy)"


# --- Чтение системного буфера -------------------------------------------------

def get_clipboard() -> Tuple[bool, str]:
    """Прочитать текущее содержимое системного буфера обмена ОС."""
    if _SYS == "Windows":  # pragma: no cover
        # Симметрично записи: PowerShell кодирует содержимое буфера в base64
        # (UTF-8), мы получаем чистый ASCII и декодируем — кодировка консоли
        # не вмешивается, кириллица не бьётся.
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            script = (
                "$s=Get-Clipboard -Raw;"
                "if($null -eq $s){$s=''};"
                "$bytes=[System.Text.Encoding]::UTF8.GetBytes($s);"
                "[Convert]::ToBase64String($bytes)"
            )
            ok, out = _run(
                [ps, "-NoProfile", "-NonInteractive", "-Command", script],
                capture=True,
            )
            if ok:
                # Оставляем только символы алфавита base64 (на случай BOM,
                # пробелов, переводов строк в выводе PowerShell).
                cleaned = "".join(_re.findall(r"[A-Za-z0-9+/=]", out))
                try:
                    text = base64.b64decode(cleaned).decode("utf-8")
                except Exception as e:  # noqa: BLE001
                    return False, f"декодирование: {e}"
                # Get-Clipboard -Raw добавляет завершающий перевод строки.
                if text.endswith("\r\n"):
                    text = text[:-2]
                elif text.endswith("\n"):
                    text = text[:-1]
                return True, text
            return False, out
        return False, "powershell не найден"
    if _SYS == "Darwin":  # pragma: no cover
        return _run(["pbpaste"], capture=True)
    for tool, args in (
        ("wl-paste", ["wl-paste", "--no-newline"]),
        ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
        ("xsel", ["xsel", "--clipboard", "--output"]),
    ):
        if shutil.which(tool):
            return _run(args, capture=True)
    return False, "буфер недоступен (нет xclip/xsel/wl-paste)"


def clipboard_available() -> bool:
    """Грубая проверка, что чтение/запись буфера в принципе возможны."""
    if _SYS == "Windows":  # pragma: no cover
        return bool(shutil.which("powershell") or shutil.which("pwsh")
                    or shutil.which("clip"))
    if _SYS == "Darwin":  # pragma: no cover
        return bool(shutil.which("pbcopy"))
    return any(shutil.which(t) for t in
               ("xclip", "xsel", "wl-copy", "wl-paste"))


# --- Картинки и файлы в буфере (Windows) -------------------------------------

def _ps() -> Optional[str]:
    return shutil.which("powershell") or shutil.which("pwsh")


def set_clipboard_image(png_bytes: bytes) -> Tuple[bool, str]:
    """Положить картинку (PNG) в системный буфер ОС, чтобы Ctrl+V вставлял её.

    Windows: PowerShell запускается с -STA (главный поток уже STA, отдельный
    поток не нужен — он как раз и мешал). Читаем base64 из stdin, декодируем в
    Bitmap и кладём через Clipboard::SetImage. Возвращаем реальную ошибку.
    """
    if _SYS == "Windows":  # pragma: no cover
        ps = _ps()
        if not ps:
            return False, "powershell не найден"
        b64 = base64.b64encode(png_bytes).decode("ascii")
        # -STA уже даёт STA-апартамент главному потоку. SetImage требует, чтобы
        # картинка жила копией (Bitmap), иначе поток-источник мог её закрыть.
        script = (
            "$ErrorActionPreference='Stop';"
            "try{"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$b64=[Console]::In.ReadToEnd();"
            "$bytes=[Convert]::FromBase64String($b64);"
            "$ms=New-Object System.IO.MemoryStream(,$bytes);"
            "$img=[System.Drawing.Image]::FromStream($ms);"
            "$bmp=New-Object System.Drawing.Bitmap($img);"
            "[System.Windows.Forms.Clipboard]::SetImage($bmp);"
            "Start-Sleep -Milliseconds 120;"
            "Write-Output 'OK'"
            "}catch{Write-Output ('ERR: '+$_.Exception.Message)}"
        )
        ok, out = _run([ps, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                       send_text=b64, capture=True)
        out = (out or "").strip()
        if not ok:
            return False, out or "powershell не отработал"
        if out.startswith("OK"):
            return True, ""
        return False, out or "буфер не принял картинку"
    # Linux: xclip умеет класть image/png из stdin
    if shutil.which("xclip"):
        return _run_bytes(["xclip", "-selection", "clipboard", "-t", "image/png"], png_bytes)
    return False, "запись картинки не поддерживается на этой ОС"


def clipboard_kind() -> str:
    """Что сейчас в буфере ОС: 'image' | 'files' | 'text' | 'empty'.

    Только Windows (для авто-слежения и умного pull). На прочих ОС — 'text'.
    """
    if _SYS != "Windows":  # pragma: no cover
        return "text"
    ps = _ps()
    if not ps:
        return "text"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "if([System.Windows.Forms.Clipboard]::ContainsImage()){'image'}"
        "elseif([System.Windows.Forms.Clipboard]::ContainsFileDropList()){'files'}"
        "elseif([System.Windows.Forms.Clipboard]::ContainsText()){'text'}"
        "else{'empty'}"
    )
    ok, out = _run([ps, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                   capture=True)
    out = (out or "").strip().lower()
    return out if out in ("image", "files", "text", "empty") else "text"


def get_clipboard_image() -> Tuple[bool, bytes | str]:
    """Прочитать картинку из буфера ОС как PNG-байты. (ok, bytes|ошибка)."""
    if _SYS != "Windows":  # pragma: no cover
        return False, "чтение картинки поддерживается только на Windows"
    ps = _ps()
    if not ps:
        return False, "powershell не найден"
    # Картинку из буфера сохраняем в PNG в MemoryStream → base64 → stdout.
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$img=[System.Windows.Forms.Clipboard]::GetImage();"
        "if($null -eq $img){''}else{"
        "$ms=New-Object System.IO.MemoryStream;"
        "$img.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png);"
        "[Convert]::ToBase64String($ms.ToArray())}"
    )
    ok, out = _run([ps, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                   capture=True)
    if not ok:
        return False, out
    cleaned = "".join(_re.findall(r"[A-Za-z0-9+/=]", out or ""))
    if not cleaned:
        return False, "в буфере нет картинки"
    try:
        return True, base64.b64decode(cleaned)
    except Exception as e:  # noqa: BLE001
        return False, f"декодирование: {e}"


def get_clipboard_files() -> Tuple[bool, list[str] | str]:
    """Прочитать список путей файлов из буфера ОС (скопированы в проводнике)."""
    if _SYS != "Windows":  # pragma: no cover
        return False, "чтение файлов поддерживается только на Windows"
    ps = _ps()
    if not ps:
        return False, "powershell не найден"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=[System.Windows.Forms.Clipboard]::GetFileDropList();"
        "if($null -eq $f){''}else{"
        "$bytes=[System.Text.Encoding]::UTF8.GetBytes(($f -join \"`n\"));"
        "[Convert]::ToBase64String($bytes)}"
    )
    ok, out = _run([ps, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                   capture=True)
    if not ok:
        return False, out
    cleaned = "".join(_re.findall(r"[A-Za-z0-9+/=]", out or ""))
    if not cleaned:
        return False, "в буфере нет файлов"
    try:
        raw = base64.b64decode(cleaned).decode("utf-8")
        paths = [p for p in raw.split("\n") if p.strip()]
        return True, paths
    except Exception as e:  # noqa: BLE001
        return False, f"декодирование: {e}"
