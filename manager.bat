@echo off
cd /d "%~dp0"

set "MODE=cli"
if /i "%~1"=="gui" set "MODE=gui"

echo ====================================
echo   TOEIC Start Manager   [%MODE%]
echo ====================================
echo.

set "VENV_PY=mvp\venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERROR] venv not found: %VENV_PY%
    echo.
    echo Run: py -3.13 -m venv mvp\venv
    echo Then: "%VENV_PY%" -m pip install -r mvp\requirements.txt
    echo.
    pause
    exit /b 1
)

REM check service deps
"%VENV_PY%" -c "import fastapi,uvicorn,httpx,dotenv,dashscope" 2>nul
if errorlevel 1 (
    echo [INFO] Installing service dependencies...
    "%VENV_PY%" -m pip install -r mvp\requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed
        echo Manual: "%VENV_PY%" -m pip install -r mvp\requirements.txt
        pause
        exit /b 1
    )
)

if "%MODE%"=="gui" (
    REM check tkinter
    "%VENV_PY%" -c "import tkinter" 2>nul
    if errorlevel 1 (
        echo [ERROR] venv missing tkinter
        echo Rebuild: rmdir /s /q mvp\venv ^&^& py -3.13 -m venv mvp\venv
        pause
        exit /b 1
    )
    "%VENV_PY%" manager.py
) else (
    "%VENV_PY%" manager.py --cli
)

echo.
echo [INFO] Manager exited (code: %ERRORLEVEL%)
pause
