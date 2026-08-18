"""
TOEIC 顽固词深度加工系统 - MVP 个人版
=========================================
单文件，零依赖基础设施。FastAPI + SQLite + DeepSeek + 百炼 TTS。

启动: pip install -r requirements.txt && cp .env.example .env && python main.py
访问: http://localhost:8000
"""

import re
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
    """从 templates/index.html 读取前端页面。

    给 /static 下的 js/css 引用自动追加版本号（基于文件修改时间），
    避免浏览器缓存旧文件导致 ES Module 加载失败（如 import 不存在的导出）。
    """
    path = TEMPLATES_DIR / "index.html"
    if not path.exists():
        return "<h1>前端文件未找到</h1>"
    html = path.read_text(encoding="utf-8")

    def _versioned(m: "re.Match[str]") -> str:
        quote, url = m.group(1), m.group(2)
        fp = STATIC_DIR / url[len("/static/"):]
        try:
            ver = int(fp.stat().st_mtime)
        except OSError:
            ver = 0
        return f"{quote}{url}?v={ver}{quote}"

    return re.sub(r'(["\'])(/static/(?:js|css)/[^"\']+)\1', _versioned, html)

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
    # no-store：HTML 永不缓存，保证每次都能拿到带最新版本号的静态资源引用
    return HTMLResponse(_load_index_html(), headers={"Cache-Control": "no-store"})

# ========================================================================
# 日志落盘：toeic.* 业务日志 JSON 单行，按天轮转
# ========================================================================

def _setup_logging():
    """给 toeic.* logger 追加按天轮转的 JSON 文件 handler。

    不做完整控制台/多格式堆叠，保持轻量：
      - 保留 services/routes 自己的 StreamHandler（走 stdout，供启动管理器 tail）
      - 这里只补一层落盘，关掉 GUI/管理器后日志不丢、可按天回查
    """
    import json as _json
    import logging
    import traceback
    from datetime import datetime as _dt
    from logging.handlers import TimedRotatingFileHandler

    logger = logging.getLogger("toeic")
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    if any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers):
        return

    class _JsonFormatter(logging.Formatter):
        def format(self, record):
            payload = {
                "ts": _dt.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "svc": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["err"] = "".join(traceback.format_exception(*record.exc_info))
            return _json.dumps(payload, ensure_ascii=False, default=str)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = TimedRotatingFileHandler(
        LOG_DIR / "app.log", when="midnight", interval=1,
        backupCount=14, encoding="utf-8",
    )
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(_JsonFormatter())
    logger.addHandler(fh)

# ========================================================================
# 启动
# ========================================================================

if __name__ == "__main__":
    import uvicorn
    _setup_logging()
    # access_log=True：保留请求日志供 manager 落盘 access.*.log 留痕；终端由 manager 里旁路处理
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info", access_log=True)
