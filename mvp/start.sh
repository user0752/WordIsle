#!/usr/bin/env bash
# WordIsle - Linux 启动脚本（等价 Windows start.bat）
set -e
cd "$(dirname "$0")"

# 1. 初始化 .env
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[提示] 已从 .env.example 复制 .env，请编辑填入 API Key"
fi
if grep -q "your_deepseek_api_key_here\|your_bailian_api_key_here" .env; then
  echo "[X] API Key 未配置，请编辑 mvp/.env"
  exit 1
fi

# 2. 检查/安装依赖（建议用 venv）
PY="${PYTHON:-python3}"
if [ ! -d "venv" ]; then
  echo "[1/3] 创建虚拟环境..."
  "$PY" -m venv venv
fi
source venv/bin/activate
python -c "import fastapi,uvicorn,httpx,dotenv,imageio_ffmpeg" 2>/dev/null || {
  echo "[2/3] 安装依赖..."
  pip install -r requirements.txt
}

# 3. 启动
mkdir -p data/audios data/images data/videos
echo "[3/3] 启动服务: http://0.0.0.0:8000 （Ctrl+C 停止）"
exec python main.py