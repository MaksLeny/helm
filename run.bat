@echo off
chcp 65001 >nul
REM ==== Helm: zapusk servera ====
REM Sbros SOCKS-proxy (inache httpx/zaprosy lomayutsya)
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set SOCKS_PROXY=

cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Okno mozhno svernut - server prodolzhit rabotat.
REM Poka server zhiv, PK ne uydet v son (anti-son vnutri main.py).
python main.py

pause
