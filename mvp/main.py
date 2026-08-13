"""
TOEIC 顽固词深度加工系统 - MVP 个人版
=========================================
单文件，零依赖基础设施。FastAPI + SQLite + DeepSeek + 百炼 TTS。

启动: pip install -r requirements.txt && cp .env.example .env && python main.py
访问: http://localhost:8000
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import *
from middleware import setup_middleware
from services import *
from db import *
from routes import router

AUDIOS_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

# ========================================================================
# 前端模板
# ========================================================================

def _load_index_html() -> str:
    """从 templates/index.html 读取前端页面。"""
    path = TEMPLATES_DIR / "index.html"
    if not path.exists():
        return "<h1>前端文件未找到</h1>"
    return path.read_text(encoding="utf-8")

# ========================================================================
# FastAPI 应用
# ========================================================================

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="TOEIC MVP", docs_url=None, redoc_url=None, lifespan=lifespan)
setup_middleware(app)

# 静态文件：前端资源 + 音频 + 图片 + 视频目录
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/audios", StaticFiles(directory=str(AUDIOS_DIR)), name="audios")
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
app.mount("/videos", StaticFiles(directory=str(VIDEOS_DIR)), name="videos")

# 注册路由
app.include_router(router)

# ========================================================================
# 前端页面
# ========================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_load_index_html())

# ========================================================================
# 启动
# ========================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
