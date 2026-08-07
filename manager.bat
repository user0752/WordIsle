@echo off
cd /d "%~dp0"

echo ====================================
echo   TOEIC Start Manager
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

REM check tkinter
"%VENV_PY%" -c "import tkinter" 2>nul
if errorlevel 1 (
    echo [ERROR] venv missing tkinter
    echo Rebuild: rmdir /s /q mvp\venv ^&^& py -3.13 -m venv mvp\venv
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

REM check manager deps
"%VENV_PY%" -c "import tkinter,json,queue,re,subprocess,threading,time,urllib.request,webbrowser,pathlib; from tkinter import filedialog,messagebox,scrolledtext,ttk" 2>nul
if errorlevel 1 (
    echo [ERROR] manager missing stdlib modules, check Python installation
    pause
    exit /b 1
)

echo Venv: %VENV_PY%
echo Starting manager GUI...
echo Close the GUI window, then press any key to exit.
echo.

"%VENV_PY%" manager.py

echo.
echo [INFO] Manager exited (code: %ERRORLEVEL%)
pause