"""
TOEIC MVP 用户系统
==================
- 全局库 system.db：users（账号）+ quotas（每日配额，跨用户维度）
- 会话：HMAC 签名 HttpOnly Cookie（无状态，不建 session 表）
- 认证依赖 get_current_user：解析 Cookie → 写入 current_uid contextvar → 返回用户信息
- 每日配额：check/consume 原子 UPSERT，dev/admin 不限量
- 认证 API：/api/login、/api/login-guest、/api/logout、/api/me + 登录页 /login

设计依据：《优化方案_用户系统与移动端适配.md》第 3 节。
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from config import *
from db import current_uid, current_user, ensure_db_initialized, setup_stream_logger

ROLE_DEV = "dev"
ROLE_ADMIN = "admin"
ROLE_GUEST = "guest"

router = APIRouter()

# 认证审计日志：登录 / 退出 / 游客进入 均记录 谁 + 何时 + 从哪个 IP，进后台日志留痕
logger = setup_stream_logger("toeic.auth")


def _client_ip(request: Request) -> str:
    """取客户端 IP：优先 x-forwarded-for（nginx 反代场景），否则直连地址。"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "-"

# ========================================================================
# 全局库（users / quotas）
# ========================================================================

def get_system_conn() -> sqlite3.Connection:
    """打开全局库连接（账号、配额跨用户维度）。"""
    SYSTEM_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SYSTEM_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-SHA256 加盐哈希，返回 salt$digest。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000)
    return f"{salt}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, _digest = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(_hash_password(password, salt), stored)


def _seed_user(conn, username: str, password: str, role: str):
    """播种开发者/管理员账号。.env 为唯一事实来源：每次启动按 .env 重写口令哈希与角色
    （改 .env 密码后重启即生效）。明文不落文档。"""
    uid = username
    conn.execute(
        "INSERT INTO users (uid, username, password_hash, role) VALUES (?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET password_hash=excluded.password_hash, role=excluded.role",
        (uid, username, _hash_password(password), role),
    )


def init_system_db():
    """初始化全局库：建表 + 播种开发者/管理员账号（幂等）。"""
    conn = get_system_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL CHECK (role IN ('dev','admin','guest')),
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS quotas (
                day    TEXT NOT NULL,   -- YYYY-MM-DD
                uid    TEXT NOT NULL,
                bucket TEXT NOT NULL,   -- video/batch/single/scene/polysemy/morpheme/extract/enrich
                cnt    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, uid, bucket)
            );
        """)
        if DEV_PASSWORD:
            _seed_user(conn, DEV_USERNAME, DEV_PASSWORD, ROLE_DEV)
        for _username, _pwd in ADMIN_USERS:
            _seed_user(conn, _username, _pwd, ROLE_ADMIN)
        conn.commit()
    finally:
        conn.close()


def get_user(uid: str) -> dict | None:
    conn = get_system_conn()
    try:
        row = conn.execute("SELECT uid, username, role FROM users WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    """按用户名查开发者/管理员账号（游客不参与表单登录）。"""
    conn = get_system_conn()
    try:
        row = conn.execute(
            "SELECT uid, username, role, password_hash FROM users "
            "WHERE username=? AND role IN ('dev','admin')",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_guest_user() -> dict:
    """新建游客：随机 uid，独立业务库文件。返回 {uid, username, role}。"""
    uid = f"guest-{uuid.uuid4().hex[:12]}"
    conn = get_system_conn()
    try:
        conn.execute(
            "INSERT INTO users (uid, username, password_hash, role) VALUES (?,?,?,?)",
            (uid, uid, "", ROLE_GUEST),
        )
        conn.commit()
    finally:
        conn.close()
    return {"uid": uid, "username": f"游客·{uid[-4:]}", "role": ROLE_GUEST}


# ========================================================================
# 会话（HMAC 签名 Cookie，无状态）
# ========================================================================

def _sign(data: str) -> str:
    return hmac.new(AUTH_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()


def create_session_token(uid: str) -> str:
    """签发形如 uid.ts.sig 的会话令牌（含签发时间，配合 AUTH_MAX_AGE 过期）。"""
    ts = int(time.time())
    payload = f"{uid}.{ts}"
    return f"{payload}.{_sign(payload)}"


def verify_session_token(token: str) -> str | None:
    """校验会话令牌；有效返回 uid，否则 None。"""
    try:
        uid, ts, sig = token.split(".")
    except ValueError:
        return None
    payload = f"{uid}.{ts}"
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        if int(time.time()) - int(ts) > AUTH_MAX_AGE:
            return None
    except ValueError:
        return None
    return uid


def _set_session_cookie(resp: Response, uid: str):
    resp.set_cookie(
        AUTH_COOKIE, create_session_token(uid),
        max_age=AUTH_MAX_AGE, httponly=True, samesite="lax", path="/",
    )


def _default_dev_user() -> dict:
    return {"uid": DEV_USERNAME, "username": DEV_USERNAME, "role": ROLE_DEV}


async def get_current_user(request: Request) -> dict:
    """认证依赖：强制登录（除放行名单外）。
    解析会话 Cookie → 校验 → 写入 current_uid / current_user contextvar → 返回用户信息。
    业务路由经 router 级 dependencies 注入本依赖。"""
    if AUTH_DISABLED:
        # 本地开发 / 回归测试：放行并返回默认开发者身份
        _user = _default_dev_user()
        current_uid.set(_user["uid"])
        current_user.set(_user)
        return _user
    if request.url.path in ("/api/health",):
        # 健康检查（监控自检）放行，身份归开发者库
        _user = _default_dev_user()
        current_uid.set(_user["uid"])
        current_user.set(_user)
        return _user
    token = request.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    if not uid:
        raise HTTPException(401, "未登录或登录已过期")
    user = get_user(uid)
    if not user:
        raise HTTPException(401, "用户不存在")
    current_uid.set(uid)
    current_user.set(user)
    ensure_db_initialized(uid)
    return user


# ========================================================================
# 每日配额
# ========================================================================

def _get_quota_used(uid: str, bucket: str) -> int:
    conn = get_system_conn()
    try:
        row = conn.execute(
            "SELECT cnt FROM quotas WHERE day=? AND uid=? AND bucket=?",
            (date.today().isoformat(), uid, bucket),
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def quota_limit(role: str, bucket: str) -> int:
    """返回 bucket 每日上限；dev/admin 不限（-1）。"""
    if role in (ROLE_DEV, ROLE_ADMIN):
        return -1
    return GUEST_LIMITS.get(bucket, -1)


def _bump_quota(uid: str, bucket: str, limit: int) -> bool:
    """原子配额累加：仅当当日已用 < limit 时 +1（BEGIN IMMEDIATE 持写锁）。
    返回是否成功（未超限并完成 +1）。"""
    conn = get_system_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        today = date.today().isoformat()
        row = conn.execute(
            "SELECT cnt FROM quotas WHERE day=? AND uid=? AND bucket=?",
            (today, uid, bucket),
        ).fetchone()
        used = row["cnt"] if row else 0
        if used >= limit:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "INSERT INTO quotas (day, uid, bucket, cnt) VALUES (?,?,?,1) "
            "ON CONFLICT(day, uid, bucket) DO UPDATE SET cnt=cnt+1",
            (today, uid, bucket),
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def require_quota(bucket: str):
    """配额拦截（生成端点入口调用）：读当前请求 uid，超限抛 429，否则消耗 1 次。
    dev/admin 不限量直接放行。"""
    uid = current_uid.get(None) or DEV_USERNAME
    user = get_user(uid)
    role = user["role"] if user else ROLE_GUEST
    limit = quota_limit(role, bucket)
    if limit < 0:
        return
    if not _bump_quota(uid, bucket, limit):
        label = QUOTA_BUCKET_LABELS.get(bucket, bucket)
        raise HTTPException(
            429,
            f"今日{label}次数已达上限（{limit} 次/日），明日 0 点重置",
        )


def get_quota_status(uid: str) -> dict:
    """返回用户各 bucket 配额状态（limit/used/remaining），供 /api/me 与前端展示。"""
    user = get_user(uid)
    role = user["role"] if user else ROLE_GUEST
    out = {}
    for bucket in GUEST_LIMITS:
        limit = quota_limit(role, bucket)
        used = _get_quota_used(uid, bucket) if limit >= 0 else 0
        out[bucket] = {
            "label": QUOTA_BUCKET_LABELS.get(bucket, bucket),
            "limit": limit,
            "used": used,
            "remaining": -1 if limit < 0 else max(0, limit - used),
        }
    return out


# ========================================================================
# 认证 API
# ========================================================================

async def _read_json(req: Request) -> dict:
    try:
        raw = await req.body()
    except Exception:
        raise HTTPException(400, "请求体读取失败")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise HTTPException(400, "请求体不是合法 JSON")
    return data if isinstance(data, dict) else {}


@router.post("/api/login")
async def login(req: Request, resp: Response):
    """账号登录：开发者 / 管理员。成功后写会话 Cookie。"""
    body = await _read_json(req)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    ip = _client_ip(req)
    if not username or not password:
        raise HTTPException(400, "请输入账号和密码")
    user = get_user_by_username(username)
    if not user or not user["password_hash"] or not _verify_password(password, user["password_hash"]):
        logger.warning("登录失败 user=%s ip=%s 原因=账号或密码错误", username, ip)
        raise HTTPException(401, "账号或密码错误")
    _set_session_cookie(resp, user["uid"])
    current_uid.set(user["uid"])
    current_user.set(user)
    ensure_db_initialized(user["uid"])
    logger.info("登录成功 user=%s uid=%s role=%s ip=%s", user["username"], user["uid"], user["role"], ip)
    return {"uid": user["uid"], "username": user["username"], "role": user["role"]}


@router.post("/api/login-guest")
async def login_guest(req: Request, resp: Response):
    """游客直接进入：分配随机 uid，写会话 Cookie（数据随浏览器保存）。"""
    user = create_guest_user()
    _set_session_cookie(resp, user["uid"])
    current_uid.set(user["uid"])
    current_user.set(user)
    ensure_db_initialized(user["uid"])
    logger.info("游客登录 uid=%s username=%s role=%s ip=%s", user["uid"], user["username"], user["role"], _client_ip(req))
    return {"uid": user["uid"], "username": user["username"], "role": user["role"]}


@router.post("/api/logout")
async def logout(req: Request, resp: Response):
    token = req.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if user:
        current_uid.set(user["uid"])
        current_user.set(user)
    resp.delete_cookie(AUTH_COOKIE, path="/")
    logger.info("退出登录 uid=%s username=%s role=%s ip=%s",
                uid or "-", (user or {}).get("username", "-"), (user or {}).get("role", "-"), _client_ip(req))
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request):
    """当前身份 + 当日剩余配额。AUTH_DISABLED 时返回默认开发者身份。"""
    if AUTH_DISABLED:
        user = _default_dev_user()
    else:
        token = request.cookies.get(AUTH_COOKIE)
        uid = verify_session_token(token) if token else None
        if not uid:
            raise HTTPException(401, "未登录或登录已过期")
        user = get_user(uid) or _default_dev_user()
        ensure_db_initialized(user["uid"])
    current_uid.set(user["uid"])
    current_user.set(user)
    return {
        "uid": user["uid"],
        "username": user["username"],
        "role": user["role"],
        "limits": get_quota_status(user["uid"]),
    }


# ========================================================================
# 登录页
# ========================================================================

def _load_login_html() -> str:
    path = Path(__file__).resolve().parent / "templates" / "login.html"
    if not path.exists():
        return "<h1>登录页文件未找到</h1>"
    return path.read_text(encoding="utf-8")


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(_load_login_html(), headers={"Cache-Control": "no-store"})
