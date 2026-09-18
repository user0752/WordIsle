"""
WordIsle MVP 用户系统
==================
- 全局库 system.db：users（账号）+ quotas（每日配额，跨用户维度）+
  sms_codes（短信验证码）/ transactions（余额流水）/ redeem_codes（充值卡密）
- 会话：HMAC 签名 HttpOnly Cookie（无状态，不建 session 表）
- 认证依赖 get_current_user：解析 Cookie → 写入 current_uid contextvar → 返回用户信息
- 配额/计费：游客按每日配额（GUEST_LIMITS）；注册用户（user）按账户余额
  扣费（BUCKET_PRICES 单价，不足 402）；dev/admin 不限量
- 认证 API：/api/login、/api/login-guest、/api/logout、/api/me、注册/升级/充值 +
  登录页 /login

设计依据：《优化方案_用户系统与移动端适配.md》第 3 节。
"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from config import *
from db import current_uid, current_user, ensure_db_initialized, migrate_user_db, setup_stream_logger

ROLE_DEV = "dev"
ROLE_ADMIN = "admin"
ROLE_GUEST = "guest"
ROLE_USER = "user"

router = APIRouter()

# 认证审计日志：登录 / 退出 / 游客进入 均记录 谁 + 何时 + 从哪个 IP，进后台日志留痕
logger = setup_stream_logger("wordisle.auth")


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


def _migrate_users_table(conn):
    """幂等迁移 users 表：新增 phone / phone_verified / balance 列，role 扩为 user。
    未迁移前迁移；已含 phone 列则跳过。对齐 _migrate_words_table 的建新表→搬运→切换风格。"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "phone" in cols:
        return
    conn.executescript("""
        CREATE TABLE users_new (
            uid TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL CHECK (role IN ('dev','admin','guest','user')),
            phone TEXT DEFAULT '',
            phone_verified INTEGER DEFAULT 0,
            balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO users_new (uid, username, password_hash, role, created_at)
            SELECT uid, username, password_hash, role, created_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;
    """)


def _migrate_users_account_cols(conn):
    """幂等迁移 users 表：新增 status（active/banned）/ nickname / banned_at 列。
    已含 status 列则跳过。与 _migrate_users_table 同风格。"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "status" in cols:
        return
    conn.executescript("""
        CREATE TABLE users_new (
            uid TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL CHECK (role IN ('dev','admin','guest','user')),
            phone TEXT DEFAULT '',
            phone_verified INTEGER DEFAULT 0,
            balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            status TEXT NOT NULL DEFAULT 'active',
            nickname TEXT NOT NULL DEFAULT '',
            banned_at TEXT DEFAULT ''
        );
        INSERT INTO users_new (uid, username, password_hash, role, phone, phone_verified, balance, created_at)
            SELECT uid, username, password_hash, role, phone, phone_verified, balance, created_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;
    """)


def init_system_db():
    """初始化全局库：建表 + 播种开发者/管理员账号（幂等）。"""
    conn = get_system_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL CHECK (role IN ('dev','admin','guest','user')),
                phone TEXT DEFAULT '',
                phone_verified INTEGER DEFAULT 0,
                balance INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                status TEXT NOT NULL DEFAULT 'active',
                nickname TEXT NOT NULL DEFAULT '',
                banned_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS quotas (
                day    TEXT NOT NULL,   -- YYYY-MM-DD
                uid    TEXT NOT NULL,
                bucket TEXT NOT NULL,   -- video/batch/single/scene/polysemy/morpheme/extract/enrich
                cnt    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, uid, bucket)
            );
            CREATE TABLE IF NOT EXISTS sms_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                type TEXT DEFAULT 'register',
                expire_at INTEGER NOT NULL,
                used INTEGER DEFAULT 0,
                try_count INTEGER DEFAULT 0,
                ip TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_codes(phone, id);
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                type TEXT NOT NULL,          -- register_gift/upgrade_gift/redeem/consume/admin_credit
                bucket TEXT DEFAULT '',
                amount INTEGER NOT NULL,     -- 正=收入 负=支出
                balance_after INTEGER NOT NULL,
                ref TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_txn_uid ON transactions(uid, id);
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                amount INTEGER NOT NULL,
                used INTEGER DEFAULT 0,
                used_by TEXT DEFAULT '',
                used_at TEXT DEFAULT '',
                batch TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS login_fails (
                scope TEXT NOT NULL,          -- 'user' | 'ip'
                scope_key TEXT NOT NULL,
                fail_count INTEGER DEFAULT 0,
                locked_until INTEGER DEFAULT 0,   -- epoch 秒
                updated_at INTEGER DEFAULT 0,     -- epoch 秒
                PRIMARY KEY (scope, scope_key)
            );
        """)
        _migrate_users_table(conn)
        _migrate_users_account_cols(conn)
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
        row = conn.execute(
            "SELECT uid, username, role, phone, balance, status, nickname FROM users WHERE uid=?", (uid,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    """按用户名查开发者/管理员/注册用户账号（游客不参与表单登录）。"""
    conn = get_system_conn()
    try:
        row = conn.execute(
            "SELECT uid, username, role, password_hash, phone, balance, status, nickname FROM users "
            "WHERE username=? AND role IN ('dev','admin','user')",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_phone(phone: str) -> dict | None:
    """按手机号查注册用户账号（admin/user 均可能，admin 播种无 phone）。"""
    conn = get_system_conn()
    try:
        row = conn.execute(
            "SELECT uid, username, role, password_hash, phone, balance, phone_verified, status, nickname FROM users "
            "WHERE phone=? AND role IN ('admin','user')",
            (phone,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ========================================================================
# 账户状态：封禁
# ========================================================================

def is_banned(user: dict | None) -> bool:
    """用户是否被封禁。"""
    return bool(user and user.get("status") == "banned")


def require_not_banned(user: dict):
    """封禁拦截：被封禁抛 403（持久、管理员可控，区别于临时 423 锁定）。"""
    if is_banned(user):
        raise HTTPException(403, "账号已被封禁，请联系管理员")


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
    require_not_banned(user)   # 封禁用户所有认证端点 403
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


# ========================================================================
# 账户余额（注册用户按使用额度付费）
# ========================================================================

def get_balance(uid: str) -> int:
    """返回某用户当前余额（岛屿币）。用户不存在返回 0。"""
    user = get_user(uid)
    return int(user["balance"]) if user else 0


def get_transactions(uid: str, limit: int = 20) -> list[dict]:
    """返回某用户近期收支流水（倒序）。"""
    conn = get_system_conn()
    try:
        rows = conn.execute(
            "SELECT type, bucket, amount, balance_after, ref, created_at "
            "FROM transactions WHERE uid=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def credit(uid: str, amount: int, type_: str, bucket: str = "", ref: str = ""):
    """原子增加余额并写入流水（amount 正=收入 负=支出）。超出范围抛 400 防负余额。"""
    if amount == 0:
        return
    conn = get_system_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT balance FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "用户不存在")
        new_balance = int(row["balance"]) + amount
        if new_balance < 0:
            conn.execute("ROLLBACK")
            raise HTTPException(400, "余额不足")
        conn.execute(
            "UPDATE users SET balance=? WHERE uid=?", (new_balance, uid)
        )
        conn.execute(
            "INSERT INTO transactions (uid, type, bucket, amount, balance_after, ref) "
            "VALUES (?,?,?,?,?,?)",
            (uid, type_, bucket, amount, new_balance, ref),
        )
        conn.commit()
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def require_credit(bucket: str, count: int = 1):
    """注册用户余额扣费：余额 < 单价*count 抛 402；否则原子扣减并写流水。"""
    uid = current_uid.get(None) or DEV_USERNAME
    price = BUCKET_PRICES.get(bucket, 1)
    cost = price * count
    conn = get_system_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT balance FROM users WHERE uid=?", (uid,)).fetchone()
        balance = int(row["balance"]) if row else 0
        if balance < cost:
            conn.execute("ROLLBACK")
            raise HTTPException(
                402,
                f"余额不足（本次需 {cost} 岛屿币，当前余额 {balance}），请前往充值",
            )
        new_balance = balance - cost
        conn.execute("UPDATE users SET balance=? WHERE uid=?", (new_balance, uid))
        conn.execute(
            "INSERT INTO transactions (uid, type, bucket, amount, balance_after, ref) "
            "VALUES (?,?,?,?,?,?)",
            (uid, "consume", bucket, -cost, new_balance, ""),
        )
        conn.commit()
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def require_quota(bucket: str):
    """配额/余额拦截（生成端点入口调用）：
    - 封禁用户（banned）一律 403 拒绝
    - dev/admin 不限量直接放行
    - 注册用户（user）按余额扣费（require_credit）
    - 游客（guest）按每日配额消耗 1 次（超限 429）"""
    uid = current_uid.get(None) or DEV_USERNAME
    user = get_user(uid)
    require_not_banned(user)   # 纵深防御：封禁用户在生成入口被拦（认证依赖已拦一次）
    role = user["role"] if user else ROLE_GUEST
    if role == ROLE_USER:
        require_credit(bucket)
        return
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
    """返回用户各 bucket 配额状态（limit/used/remaining），供 /api/me 与前端展示。
    注册用户（user）余额制：limit 展示为「按当前余额可生成的估算次数」（balance/单价），
    不设每日配额；dev/admin 不限（-1）。"""
    user = get_user(uid)
    role = user["role"] if user else ROLE_GUEST
    balance = int(user["balance"]) if user else 0
    out = {}
    for bucket in GUEST_LIMITS:
        if role == ROLE_USER:
            price = BUCKET_PRICES.get(bucket, 1)
            out[bucket] = {
                "label": QUOTA_BUCKET_LABELS.get(bucket, bucket),
                "limit": -1 if price == 0 else balance // price,
                "used": 0,
                "remaining": -1 if price == 0 else balance // price,
            }
            continue
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
# 登录安全：失败递进锁定（账号级 + IP 级，落库可审计）
# ========================================================================

def _lock_duration(fail_count: int) -> int:
    """按累计失败次数返回锁定时长（秒）；未达任何阈值返回 0。"""
    duration = 0
    for threshold, lock_sec in LOGIN_LOCK_THRESHOLDS:
        if fail_count >= threshold:
            duration = lock_sec
    return duration


def _lock_remaining(scope: str, scope_key: str) -> int:
    """返回 scope 条目剩余锁定秒数；未锁定/已过期返回 0。"""
    now = int(time.time())
    try:
        conn = get_system_conn()
        try:
            row = conn.execute(
                "SELECT locked_until, fail_count FROM login_fails WHERE scope=? AND scope_key=?",
                (scope, scope_key),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return 0
    if not row or not row["locked_until"]:
        return 0
    remaining = int(row["locked_until"]) - now
    return max(remaining, 0)


def _record_login_fail(scope: str, scope_key: str):
    """累计一次失败并写入用量计数；达阈值则设置 locked_until。"""
    now = int(time.time())
    try:
        conn = get_system_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT fail_count, locked_until FROM login_fails WHERE scope=? AND scope_key=?",
                (scope, scope_key),
            ).fetchone()
            fail_count = int(row["fail_count"]) + 1 if row else 1
            locked_until = 0
            duration = _lock_duration(fail_count)
            if duration:
                locked_until = now + duration
            conn.execute(
                "INSERT INTO login_fails (scope, scope_key, fail_count, locked_until, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(scope, scope_key) DO UPDATE SET "
                "fail_count=excluded.fail_count, locked_until=excluded.locked_until, updated_at=excluded.updated_at",
                (scope, scope_key, fail_count, locked_until, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _clear_login_fail(scope: str, scope_key: str):
    """成功后清零记录（幂等）。"""
    try:
        conn = get_system_conn()
        try:
            conn.execute("DELETE FROM login_fails WHERE scope=? AND scope_key=?", (scope, scope_key))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _check_login_lock(scope: str, scope_key: str):
    """入口锁定检查：剩余锁定时长 > 0 抛 423 + Retry-After 头。
    锁定期间的每一次尝试也累计失败次数（防攻击者等锁期结束后从低档重新开始）。"""
    remaining = _lock_remaining(scope, scope_key)
    if remaining > 0:
        _record_login_fail(scope, scope_key)  # 锁期内继续累计，递进到更高档
        remaining = _lock_remaining(scope, scope_key)
        exc = HTTPException(423, f"尝试次数过多，已临时锁定，请 {remaining // 60 + 1} 分钟后再试")
        exc.headers = {"Retry-After": str(max(remaining, 1)), "X-Lock-Remaining": str(remaining)}
        raise exc


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
    """账号登录：开发者 / 管理员 / 注册用户（支持用户名或手机号）。成功后写会话 Cookie。
    安全：失败递进锁定（账号级 + IP 级），封禁用户拒绝登录。"""
    body = await _read_json(req)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    ip = _client_ip(req)
    if not username or not password:
        raise HTTPException(400, "请输入账号和密码")
    _check_login_lock("ip", ip)
    _check_login_lock("user", username)
    user = get_user_by_username(username) or get_user_by_phone(username)
    if is_banned(user):
        logger.warning("封禁账号尝试登录 user=%s ip=%s", username, ip)
        raise HTTPException(403, "账号已被封禁，请联系管理员")
    if not user or not user["password_hash"] or not _verify_password(password, user["password_hash"]):
        _record_login_fail("user", username)
        _record_login_fail("ip", ip)
        logger.warning("登录失败 user=%s ip=%s 原因=账号或密码错误", username, ip)
        raise HTTPException(401, "账号或密码错误")
    _clear_login_fail("user", username)
    _clear_login_fail("ip", ip)
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
    """当前身份 + 当日剩余配额 + 账户余额。AUTH_DISABLED 时返回默认开发者身份。"""
    if AUTH_DISABLED:
        user = _default_dev_user()
    else:
        token = request.cookies.get(AUTH_COOKIE)
        uid = verify_session_token(token) if token else None
        if not uid:
            raise HTTPException(401, "未登录或登录已过期")
        user = get_user(uid) or _default_dev_user()
        require_not_banned(user)
        ensure_db_initialized(user["uid"])
    current_uid.set(user["uid"])
    current_user.set(user)
    return {
        "uid": user["uid"],
        "username": user["username"],
        "role": user["role"],
        "status": user.get("status", "active"),
        "nickname": user.get("nickname", ""),
        "phone": user.get("phone", ""),
        "balance": get_balance(user["uid"]),
        "limits": get_quota_status(user["uid"]),
    }


# ========================================================================
# 个人中心：密码 / 换绑 / 昵称
# ========================================================================

@router.post("/api/password/change")
async def password_change(req: Request):
    """登录态修改密码：旧密码校验 + 新密码（≥8 位）。"""
    token = req.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    require_not_banned(user)
    body = await _read_json(req)
    old_password = str(body.get("old_password", ""))
    new_password = str(body.get("new_password", ""))
    if not old_password or not new_password:
        raise HTTPException(400, "请输入旧密码和新密码")
    if len(new_password) < 8:
        raise HTTPException(400, "新密码长度至少 8 位")
    if new_password == old_password:
        raise HTTPException(400, "新密码不能与旧密码相同")
    # get_user 不返回 password_hash（避免审计日志误带出），单独取哈希
    detail = get_user_by_username(user["username"]) or get_user_by_phone(user.get("phone", "")) or {}
    if not detail.get("password_hash") or not _verify_password(old_password, detail["password_hash"]):
        raise HTTPException(401, "旧密码错误")
    conn = get_system_conn()
    try:
        conn.execute("UPDATE users SET password_hash=? WHERE uid=?", (_hash_password(new_password), uid))
        conn.commit()
    finally:
        conn.close()
    _clear_login_fail("user", user["username"])
    logger.info("修改密码 uid=%s", uid)
    return {"ok": True}


@router.post("/api/password/reset")
async def password_reset(req: Request, resp: Response):
    """忘记密码：短信验证码（type=reset）重置密码。不需登录态。"""
    body = await _read_json(req)
    phone = _check_phone(str(body.get("phone", "")))
    sms_code = str(body.get("sms_code", ""))
    new_password = str(body.get("new_password", ""))
    if len(new_password) < 8:
        raise HTTPException(400, "新密码长度至少 8 位")
    _verify_sms_code(phone, sms_code, "reset")
    user = get_user_by_phone(phone)
    if not user:
        raise HTTPException(404, "该手机号未注册")
    conn = get_system_conn()
    try:
        conn.execute(
            "UPDATE users SET password_hash=?, phone_verified=1, status='active' WHERE uid=?",
            (_hash_password(new_password), user["uid"]),
        )
        conn.commit()
    finally:
        conn.close()
    _clear_login_fail("user", user["username"])
    _set_session_cookie(resp, user["uid"])
    current_uid.set(user["uid"])
    current_user.set(user)
    logger.info("短信重置密码 uid=%s phone=%s", user["uid"], phone)
    return {"ok": True, "uid": user["uid"]}


@router.post("/api/phone/rebind")
async def phone_rebind(req: Request):
    """登录态手机号换绑：旧手机码 + 新手机码双验证（type=rebind）。"""
    token = req.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    require_not_banned(user)
    if not user.get("phone"):
        raise HTTPException(400, "当前账号未绑定手机号")
    body = await _read_json(req)
    old_sms_code = str(body.get("old_sms_code", ""))
    new_phone = str(body.get("new_phone", "")).strip()
    new_sms_code = str(body.get("new_sms_code", ""))
    new_phone = _check_phone(new_phone)
    if new_phone == user["phone"]:
        raise HTTPException(400, "新手机号与当前一致")
    if get_user_by_phone(new_phone):
        raise HTTPException(409, "该手机号已被其他账号绑定")
    _verify_sms_code(user["phone"], old_sms_code, "rebind")
    _verify_sms_code(new_phone, new_sms_code, "rebind")
    conn = get_system_conn()
    try:
        # username 同步为新手机号（注册时 username=phone，换绑应一并更新，
        # 否则旧手机号仍可作为登录账号使用）
        conn.execute(
            "UPDATE users SET phone=?, username=?, phone_verified=1 WHERE uid=?",
            (new_phone, new_phone, uid),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("手机号换绑 uid=%s 旧=%s 新=%s", uid, user["phone"], new_phone)
    return {"ok": True, "phone": new_phone}


@router.post("/api/profile/nickname")
async def profile_nickname(req: Request):
    """登录态修改昵称（1~16 字符）。"""
    token = req.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    require_not_banned(user)
    body = await _read_json(req)
    nickname = str(body.get("nickname", "")).strip()
    if not (1 <= len(nickname) <= 16):
        raise HTTPException(400, "昵称长度需为 1~16 字符")
    conn = get_system_conn()
    try:
        conn.execute("UPDATE users SET nickname=? WHERE uid=?", (nickname, uid))
        conn.commit()
    finally:
        conn.close()
    logger.info("修改昵称 uid=%s nickname=%s", uid, nickname)
    return {"ok": True, "nickname": nickname}


# ========================================================================
# 手机号注册 / 图形验证码 / 短信验证码 / 余额充值
# ========================================================================

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _check_phone(phone: str) -> str:
    """校验手机号格式，非法抛 400。"""
    phone = (phone or "").strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "手机号格式不正确")
    return phone


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


@router.get("/api/captcha")
async def captcha():
    """滑块拼图验证码：返回 {captcha_id, width, height, piece_size, bg(带洞背景), piece(拼图块)}。
    目标 x 坐标仅存服务端内存，不下发前端（位图无法被脚本直接解析缺口）。"""
    from verification import generate_captcha
    return generate_captcha()


def _sms_sent_count(phone: str, ip: str, since_ts: int) -> tuple[int, int, str]:
    """统计某手机/某 IP 在 since_ts 之后的发送次数，并返回最近一次发送时间（防冷却）。"""
    conn = get_system_conn()
    try:
        phone_cnt = conn.execute(
            "SELECT COUNT(*) c FROM sms_codes WHERE phone=? AND created_at>=?",
            (phone, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_ts))),
        ).fetchone()["c"]
        ip_cnt = conn.execute(
            "SELECT COUNT(*) c FROM sms_codes WHERE ip=? AND created_at>=?",
            (ip, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_ts))),
        ).fetchone()["c"]
        last_row = conn.execute(
            "SELECT MAX(created_at) t FROM sms_codes WHERE phone=? AND ip=?",
            (phone, ip),
        ).fetchone()
        last_at = last_row["t"] or ""
        return phone_cnt, ip_cnt, last_at
    finally:
        conn.close()


@router.post("/api/sms/send")
async def sms_send(req: Request):
    """短信验证码发送：图形码校验（一次性）+ 防刷（60s 冷却 / 手机日限 / IP 日限）。"""
    from verification import verify_captcha
    from sms import send_sms_code, TEST_MODE_CODE

    body = await _read_json(req)
    phone = _check_phone(str(body.get("phone", "")))
    captcha_id = str(body.get("captcha_id", "")).strip()
    captcha_x = str(body.get("captcha_x", "")).strip()
    sms_type = str(body.get("type", "register")).strip() or "register"
    if sms_type not in SMS_SEND_TYPES:
        sms_type = "register"
    ip = _client_ip(req)
    _check_login_lock("ip", ip)   # 防图形验证码暴破短信通道：IP 级锁定同样拦截
    if not captcha_id or captcha_x == "":
        raise HTTPException(401, "缺少图形验证码")
    if not verify_captcha(captcha_id, captcha_x):
        raise HTTPException(400, "图形验证码错误或已过期")

    now = int(time.time())
    day_start = int(time.mktime(time.strptime(date.today().isoformat(), "%Y-%m-%d")))
    day_phone, day_ip, last_at = _sms_sent_count(phone, ip, day_start)
    if day_phone >= SMS_DAILY_PER_PHONE:
        raise HTTPException(429, "该手机号今日短信发送次数已达上限")
    if day_ip >= SMS_DAILY_PER_IP:
        raise HTTPException(429, "今日短信发送次数已达上限，请稍后再试")
    if last_at:
        try:
            last_ts = time.mktime(time.strptime(last_at, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            last_ts = 0
        if now - last_ts < SMS_COOLDOWN_SECONDS:
            raise HTTPException(429, "发送过于频繁，请稍后再试")

    # 生成 6 位验证码：测试模式（未配置短信）用固定码，仍走 sms_codes 落库与校验，
    # 保证注册/升级必须依赖「已发送短信」记录（图形码+频率限制），无法绕开直报。
    from sms import sms_configured
    if sms_configured():
        code = "".join(secrets.choice("0123456789") for _ in range(6))
    else:
        code = TEST_MODE_CODE
    try:
        real_sent = send_sms_code(phone, code)
    except Exception as e:
        logger.warning("短信发送异常 phone=%s err=%s ip=%s", phone, e, ip)
        real_sent = False

    conn = get_system_conn()
    try:
        conn.execute(
            "INSERT INTO sms_codes (phone, code_hash, type, expire_at, try_count, ip) "
            "VALUES (?,?,?,?,0,?)",
            (phone, _hash_code(code), sms_type, now + SMS_CODE_TTL, ip),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("短信验证码已生成 phone=%s ip=%s 真实发送=%s", phone, ip, real_sent)
    return {"ok": True, "test_mode": not real_sent, "ttl": SMS_CODE_TTL}


def _verify_sms_code(phone: str, code: str, expected_type: str = "register"):
    """校验短信验证码：必须有发送记录（sms_codes），匹配未用、未过期、尝试未超限。
    校验失败累积 try_count，超过上限作废。成功则标记 used=1 并返回 True。
    统一走库校验（测试模式固定码 123456 也先由 /api/sms/send 落库），
    无法绕过图形码/频率限制直接注册。"""
    code = (code or "").strip()
    if not code:
        raise HTTPException(400, "请输入短信验证码")
    conn = get_system_conn()
    try:
        # 单事务原子：读取+标记 used / 累积 try_count 防并发同码双用
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, code_hash, expire_at, used, try_count FROM sms_codes "
            "WHERE phone=? AND type=? ORDER BY id DESC LIMIT 1",
            (phone, expected_type),
        ).fetchone()
        if not row or row["used"]:
            conn.execute("ROLLBACK")
            raise HTTPException(400, "短信验证码不存在或已使用，请重新获取")
        if int(time.time()) > row["expire_at"]:
            conn.execute("ROLLBACK")
            raise HTTPException(400, "短信验证码已过期，请重新获取")
        if int(row["try_count"]) >= SMS_MAX_TRIES:
            conn.execute("ROLLBACK")
            raise HTTPException(400, "验证码尝试次数过多，请重新获取")
        if row["code_hash"] != _hash_code(code):
            conn.execute("UPDATE sms_codes SET try_count=try_count+1 WHERE id=?", (row["id"],))
            conn.commit()
            raise HTTPException(400, "短信验证码错误")
        conn.execute("UPDATE sms_codes SET used=1 WHERE id=?", (row["id"],))
        conn.commit()
        return True
    finally:
        conn.close()


def _register_user(phone: str, password: str, role: str = ROLE_USER, upgrade_from: str | None = None) -> dict:
    """创建正式账号（角色 user），初始化业务库，赠初始体验额度，返回用户信息。"""
    if len(password) < 8:
        raise HTTPException(400, "密码长度至少 8 位")
    if get_user_by_phone(phone):
        raise HTTPException(409, "该手机号已注册")
    uid = f"u-{uuid.uuid4().hex[:12]}"
    nickname = f"用户{phone[-4:]}"
    conn = get_system_conn()
    try:
        conn.execute(
            "INSERT INTO users (uid, username, password_hash, role, phone, phone_verified, balance, nickname) "
            "VALUES (?,?,?,?,?,1,0,?)",
            (uid, phone, _hash_password(password), role, phone, nickname),
        )
        conn.commit()
    finally:
        conn.close()
    ensure_db_initialized(uid)
    migrated = False
    try:
        if upgrade_from:
            migrate_user_db(upgrade_from, uid)
            migrated = True
            credit(uid, UPGRADE_GIFT, "upgrade_gift", ref=f"upgrade_from={upgrade_from}")
            logger.info("游客升级 uid=%s 由 %s 迁移并赠 %s 币", uid, upgrade_from, UPGRADE_GIFT)
        else:
            credit(uid, REGISTER_GIFT, "register_gift")
            logger.info("新用户注册 uid=%s phone=%s 赠 %s 币", uid, phone, REGISTER_GIFT)
    except Exception:
        if not migrated:
            # 迁移尚未成功（旧库仍在）：补偿删除刚建账号，用户可重新发起注册/升级
            try:
                conn2 = get_system_conn()
                try:
                    conn2.execute("DELETE FROM users WHERE uid=?", (uid,))
                    conn2.commit()
                finally:
                    conn2.close()
            except Exception:
                pass
            logger.warning("注册/升级失败已补偿回滚 uid=%s phone=%s", uid, phone)
            raise
        # 迁移已成功（数据已落新库、旧库已删除）：保账号可登录，仅告警不删除避免数据孤儿
        logger.warning("升级数据已迁移但赠币失败 uid=%s phone=%s", uid, phone)
    user = get_user(uid) or {"uid": uid, "username": phone, "role": role}
    return user


@router.post("/api/register")
async def register(req: Request, resp: Response):
    """手机号注册：短信验证码 + 图形验证码校验通过后创建账号并登录。"""
    body = await _read_json(req)
    phone = _check_phone(str(body.get("phone", "")))
    password = str(body.get("password", ""))
    sms_code = str(body.get("sms_code", ""))
    _verify_sms_code(phone, sms_code, "register")
    user = _register_user(phone, password)
    _set_session_cookie(resp, user["uid"])
    current_uid.set(user["uid"])
    current_user.set(user)
    ensure_db_initialized(user["uid"])
    return {"uid": user["uid"], "username": user["username"], "role": user["role"], "balance": get_balance(user["uid"])}


@router.post("/api/guest/upgrade")
async def guest_upgrade(req: Request, resp: Response):
    """游客升级：当前会话游客 + 短信验证码 → 建立正式账号并迁移学习数据。"""
    token = req.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if not user or user["role"] != ROLE_GUEST:
        raise HTTPException(403, "仅游客身份可升级")
    body = await _read_json(req)
    phone = _check_phone(str(body.get("phone", "")))
    password = str(body.get("password", ""))
    sms_code = str(body.get("sms_code", ""))
    _verify_sms_code(phone, sms_code, "guest_upgrade")
    new_user = _register_user(phone, password, upgrade_from=user["uid"])
    _set_session_cookie(resp, new_user["uid"])
    current_uid.set(new_user["uid"])
    current_user.set(new_user)
    ensure_db_initialized(new_user["uid"])
    return {
        "uid": new_user["uid"], "username": new_user["username"], "role": new_user["role"],
        "balance": get_balance(new_user["uid"]),
    }


@router.post("/api/redeem")
async def redeem(req: Request):
    """卡密兑换：校验当前登录用户 + 未使用卡密 → 加余额、写流水、标记卡密。

    单事务原子完成（BEGIN IMMEDIATE 持写锁，防并发同卡密双花）：
    校验 used → 标记 used → 用户加余额 → 写流水，任一步失败整体回滚。
    """
    body = await _read_json(req)
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(400, "请输入充值卡密")
    token = req.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    conn = get_system_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount, used FROM redeem_codes WHERE code=?", (code,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "卡密不存在")
        if row["used"]:
            conn.execute("ROLLBACK")
            raise HTTPException(400, "卡密已被使用")
        bal_row = conn.execute("SELECT balance FROM users WHERE uid=?", (uid,)).fetchone()
        if not bal_row:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "用户不存在")
        amount = int(row["amount"])
        new_balance = int(bal_row["balance"]) + amount
        conn.execute(
            "UPDATE redeem_codes SET used=1, used_by=?, used_at=? WHERE code=?",
            (uid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), code),
        )
        conn.execute("UPDATE users SET balance=? WHERE uid=?", (new_balance, uid))
        conn.execute(
            "INSERT INTO transactions (uid, type, bucket, amount, balance_after, ref) "
            "VALUES (?,?,?,?,?,?)",
            # ref 只存卡密后 4 位便于对账，不落完整卡密防泄露复用
            (uid, "redeem", "", amount, new_balance, f"redeem=****{code[-4:]}"),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("卡密兑换 uid=%s code=****%s 充值%d币", user["uid"], code[-4:], amount)
    return {"ok": True, "amount": amount, "balance": get_balance(user["uid"])}


@router.get("/api/billing")
async def billing(request: Request):
    """当前用户余额 + 各 bucket 单价 + 近期流水。"""
    token = request.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return {
        "uid": user["uid"],
        "role": user["role"],
        "balance": get_balance(user["uid"]),
        "prices": BUCKET_PRICES,
        "transactions": get_transactions(user["uid"]),
    }


# ========================================================================
# 管理员：充值卡密生成 / 手动充值 / 用户与账单查询
# ========================================================================

def _require_admin(request: Request) -> dict:
    """要求当前会话为 dev/admin，否则 401/403。"""
    token = request.cookies.get(AUTH_COOKIE)
    uid = verify_session_token(token) if token else None
    user = get_user(uid) if uid else None
    if not uid or not user:
        raise HTTPException(401, "未登录或登录已过期")
    if user["role"] not in (ROLE_DEV, ROLE_ADMIN):
        raise HTTPException(403, "无权限，仅开发者/管理员可操作")
    return user


@router.post("/api/admin/codes/generate")
async def admin_generate_codes(req: Request):
    """生成一批充值卡密：{batch, amount, count} → [{code}, ...]。"""
    user = _require_admin(req)
    body = await _read_json(req)
    batch = str(body.get("batch", "")).strip() or "S"
    try:
        amount = int(body.get("amount", 0))
        count = int(body.get("count", 0))
    except ValueError:
        raise HTTPException(400, "金额/数量必须为整数")
    if amount <= 0 or count <= 0 or count > 500:
        raise HTTPException(400, "金额>0 且数量 1~500")
    codes = []
    conn = get_system_conn()
    try:
        attempts = 0
        while len(codes) < count and attempts < count * 20:  # 防极低概率碰撞导致死循环
            attempts += 1
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            cur = conn.execute(
                "INSERT OR IGNORE INTO redeem_codes (code, amount, batch) VALUES (?,?,?)",
                (code, amount, batch),
            )
            if cur.rowcount > 0:
                codes.append(code)
        conn.commit()
    finally:
        conn.close()
    logger.info("管理员生成卡密 user=%s batch=%s 金额=%d 数量=%d", user["uid"], batch, amount, count)
    return {"ok": True, "codes": codes, "batch": batch, "amount": amount}


@router.post("/api/admin/credit")
async def admin_credit(req: Request):
    """管理员给指定用户手动充值：{uid, amount, note}。"""
    user = _require_admin(req)
    body = await _read_json(req)
    uid = str(body.get("uid", "")).strip()
    try:
        amount = int(body.get("amount", 0))
    except ValueError:
        raise HTTPException(400, "金额必须为整数")
    note = str(body.get("note", "")).strip()
    if not uid or amount == 0:
        raise HTTPException(400, "缺少用户或金额为 0")
    target = get_user(uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    credit(uid, amount, "admin_credit", ref=note)
    logger.info("管理员充值 user=%s 目标=%s 金额=%d note=%s", user["uid"], uid, amount, note)
    return {"ok": True, "uid": uid, "balance": get_balance(uid)}


@router.get("/api/admin/users")
async def admin_users(request: Request):
    """管理员查看注册用户列表：uid/用户名/角色/手机号/余额/状态/注册时间。"""
    user = _require_admin(request)
    conn = get_system_conn()
    try:
        rows = conn.execute(
            "SELECT uid, username, role, phone, balance, status, nickname, created_at FROM users "
            "WHERE role IN ('user','guest') ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        return {"users": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/api/admin/users/{target_uid}/ban")
async def admin_ban_user(target_uid: str, req: Request):
    """管理员封禁账号（仅 dev/admin）。封禁后拒绝登录与全部生成请求。"""
    operator = _require_admin(req)
    target = get_user(target_uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target["role"] in (ROLE_DEV, ROLE_ADMIN):
        raise HTTPException(400, "不能封禁开发者/管理员账号")
    conn = get_system_conn()
    try:
        conn.execute(
            "UPDATE users SET status='banned', banned_at=? WHERE uid=?",
            (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), target_uid),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("封禁账号 operator=%s target=%s(%s)", operator["uid"], target_uid, target.get("username"))
    return {"ok": True, "uid": target_uid, "status": "banned"}


@router.post("/api/admin/users/{target_uid}/unban")
async def admin_unban_user(target_uid: str, req: Request):
    """管理员解封账号（仅 dev/admin）。"""
    operator = _require_admin(req)
    target = get_user(target_uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    conn = get_system_conn()
    try:
        conn.execute(
            "UPDATE users SET status='active', banned_at='' WHERE uid=?",
            (target_uid,),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("解封账号 operator=%s target=%s(%s)", operator["uid"], target_uid, target.get("username"))
    return {"ok": True, "uid": target_uid, "status": "active"}


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
