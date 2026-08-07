@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ====================================
echo   TOEIC Word Processor - MVP
echo ====================================

REM 1. 检查 .env
if not exist ".env" copy .env.example .env >nul
findstr "your_deepseek_api_key_here" .env >nul 2>&1
if not errorlevel 1 goto :nokey
findstr "your_bailian_api_key_here" .env >nul 2>&1
if not errorlevel 1 goto :nokey
goto :checkdeps

:nokey
echo.
echo [X] API Key 未配置！
echo     请编辑 mvp\.env 文件，替换以下两行为真实密钥：
echo       DEEPSEEK_API_KEY=你的key
echo       TTS_API_KEY=你的key
echo.
pause
exit /b

:checkdeps
echo [1/2] 检查依赖...
python -c "import fastapi,uvicorn,httpx,dotenv" 2>nul
if not errorlevel 1 (
    echo       OK
    goto :start
)
echo       安装中...
python -m pip install --user -q fastapi "uvicorn[standard]" httpx python-dotenv
if errorlevel 1 (
    echo [X] 安装失败，请检查网络或手动执行:
    echo     pip install --user fastapi "uvicorn[standard]" httpx python-dotenv
    pause
    exit /b
)
echo       OK

:start
mkdir "data\audios" 2>nul
echo [2/2] 启动服务器...
echo.
echo      打开浏览器: http://localhost:8000
echo      按 Ctrl+C  停止
echo.
python main.py
pause
