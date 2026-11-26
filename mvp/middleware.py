"""
WordIsle MVP 中间件
================
FastAPI 中间件配置，统一管理：
- CORS（默认只允许同源，跨域需显式配置 CORS_ORIGINS）
- 安全响应头（nosniff / 同源 frame / referrer）
- 轻量限流（仅 /api/，按 IP 每分钟固定窗口；本机与测试客户端豁免）

限流用纯 ASGI 实现，不缓冲响应体，避免影响 SSE 流式输出。
"""

import threading
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import CORS_ORIGINS, RATE_LIMIT_CAPTCHA_PER_MINUTE, RATE_LIMIT_PER_MINUTE
from netutil import client_ip, is_local_ip

_WINDOW_SECONDS = 60
_hits: dict[tuple[str, str], tuple[float, int]] = {}
_hits_lock = threading.Lock()


def _allow_request(ip: str, bucket: str, limit: int) -> bool:
    """固定窗口计数：同一 IP 同一桶在 60 秒内最多 limit 次。"""
    now = time.time()
    with _hits_lock:
        if len(_hits) > 20000:   # 防内存膨胀：清理过期窗口
            for k, (start, _c) in list(_hits.items()):
                if now - start >= _WINDOW_SECONDS:
                    _hits.pop(k, None)
        key = (ip, bucket)
        start, count = _hits.get(key, (now, 0))
        if now - start >= _WINDOW_SECONDS:
            start, count = now, 0
        count += 1
        _hits[key] = (start, count)
        return count <= limit


class RateLimitMiddleware:
    """按 IP 限制 /api/ 请求频率；静态资源不受影响。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = request.url.path
        if not path.startswith("/api/"):
            return await self.app(scope, receive, send)

        ip = client_ip(request)
        if is_local_ip(ip):   # 本地开发 / 回归测试不限制
            return await self.app(scope, receive, send)

        limit = RATE_LIMIT_CAPTCHA_PER_MINUTE if path == "/api/captcha" else RATE_LIMIT_PER_MINUTE
        bucket = "captcha" if path == "/api/captcha" else "api"
        if not _allow_request(ip, bucket, limit):
            response = JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
            response.headers["Retry-After"] = str(_WINDOW_SECONDS)
            return await response(scope, receive, send)

        return await self.app(scope, receive, send)


class SameOriginGuardMiddleware:
    """CSRF 兜底：对写操作校验 Origin 与 Host 同源。

    SameSite=Lax 已能挡住绝大多数跨站 POST；这里再加一层，防止未来改 Cookie 策略
    或浏览器行为变化后出现空窗。无 Origin 的非浏览器客户端（curl/监控）不受影响。
    """

    _UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in self._UNSAFE_METHODS:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        origin = headers.get("origin", "").strip()
        if not origin:
            return await self.app(scope, receive, send)

        host = headers.get("host", "").strip()
        allowed = set(CORS_ORIGINS)
        if host:
            allowed |= {f"http://{host}", f"https://{host}"}
        if origin not in allowed:
            response = JSONResponse({"detail": "跨站请求已被拒绝"}, status_code=403)
            return await response(scope, receive, send)

        return await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """给所有响应追加基础安全头（不改动响应体，兼容 SSE）。"""

    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        # 只加不限制脚本/样式的部分，避免破坏 Vue 内联模板
        "Content-Security-Policy": "frame-ancestors 'self'; object-src 'none'; base-uri 'self'",
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self._HEADERS.items():
                    headers.setdefault(key, value)
            await send(message)

        return await self.app(scope, receive, send_with_headers)


def setup_middleware(app: FastAPI) -> None:
    """为应用注册所有中间件（后注册的更靠外层）。"""
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SameOriginGuardMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
