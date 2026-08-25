"""
TOEIC 顽固词深度加工系统 - MVP 个人版
=========================================
单文件，零依赖基础设施。FastAPI + SQLite + DeepSeek + 百炼 TTS。

启动: pip install -r requirements.txt && cp .env.example .env && python main.py
访问: http://localhost:8000
"""

import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import *
from middleware import setup_middleware
from services import *
from db import *
from auth import router as auth_router
from auth import init_system_db
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

# 旧全局库（迁移源）在模块导入时固化，避免被测试覆盖 DB_PATH 后指向错误源文件
_LEGACY_DB_PATH = DB_PATH


def _migrate_legacy_dev_db():
    """首次启动：把旧全局 words.db 复制为开发者库 data/user/dev-wordisle.db（原样迁移）。
    幂等：目标库已存在则跳过。可用环境变量 MIGRATE_LEGACY_DB=0 关闭（回归测试用）。"""
    target = USER_DATA_DIR / "dev-wordisle.db"
    if not _LEGACY_DB_PATH.exists() or target.exists():
        return
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(_LEGACY_DB_PATH, target)
        print(f"[migrate] 旧全局库 {_LEGACY_DB_PATH.name} → 开发者库 {target.name}")
    except Exception as e:
        print(f"[migrate] 迁移失败（请手动复制 {_LEGACY_DB_PATH} → {target}）：{e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("MIGRATE_LEGACY_DB", "1") == "1":
        _migrate_legacy_dev_db()
    init_system_db()        # 全局库：users / quotas + 开发者/管理员种子
    init_db(DEV_USERNAME)   # 开发者库（含旧数据迁移后的种子/表结构）
    yield

app = FastAPI(title="TOEIC MVP", docs_url=None, redoc_url=None, lifespan=lifespan)
setup_middleware(app)

# 静态文件：前端资源 + 音频 + 图片 + 视频目录
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/audios", StaticFiles(directory=str(AUDIOS_DIR)), name="audios")
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
app.mount("/videos", StaticFiles(directory=str(VIDEOS_DIR)), name="videos")

# 认证路由（/login、/api/login*、/api/me）——不强制登录
app.include_router(auth_router)
# 业务路由——router 级依赖强制登录（get_current_user）
app.include_router(router)

# ========================================================================
# 前端页面
# ========================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    # no-store：HTML 永不缓存，保证每次都能拿到带最新版本号的静态资源引用
    return HTMLResponse(_load_index_html(), headers={"Cache-Control": "no-store"})

# ========================================================================
# 日志落盘：toeic.* 业务日志 JSON 单行，按天轮转（含用户身份 uid/username/role）
# ========================================================================

def _setup_logging():
    """给 toeic.* logger 追加按天轮转的 JSON 文件 handler。

    不做完整控制台/多格式堆叠，保持轻量：
      - 保留 services/routes/auth 自己的 StreamHandler（走 stdout，供启动管理器 tail）
      - 这里只补一层落盘，关掉 GUI/管理器后日志不丢、可按天回查
      - 落盘与访问日志都带 user 身份（谁调用了什么模型/接口）
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
                "uid": getattr(record, "uid", "-"),
                "username": getattr(record, "username", "-"),
                "role": getattr(record, "role", "-"),
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
    fh.addFilter(UserLogFilter())  # 每条落盘记录都注入 uid/username/role
    logger.addHandler(fh)


def _uvicorn_log_config() -> dict:
    """自定义 uvicorn 日志配置：access 行末尾追加当前用户（uid/username/role），
    让后台能看出每个 API 请求是谁发起的（谁做了什么）。
    user 字段由 db.UserLogFilter 从 current_user contextvar 注入。"""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "user": {"()": "db.UserLogFilter"},
        },
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": ('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s '
                        'user=%(username)s(uid=%(uid)s role=%(role)s)'),
            },
        },
        "handlers": {
            "default": {"formatter": "default", "class": "logging.StreamHandler", "stream": "ext://sys.stderr"},
            "access": {"formatter": "access", "class": "logging.StreamHandler", "stream": "ext://sys.stderr"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False, "filters": ["user"]},
        },
    }

# ========================================================================
# 启动
# ========================================================================

if __name__ == "__main__":
    import uvicorn
    _setup_logging()
    # access 日志带用户身份（谁调用了什么接口）；终端由 manager 里旁路处理
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info", access_log=True,
                log_config=_uvicorn_log_config())
