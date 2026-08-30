"""
WordIsle MVP 数据库层
==================
SQLite 连接管理、表初始化、单词清洗、每日配额、参数校验。
"""

import json
import logging
import re
import sqlite3
from contextvars import ContextVar
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from config import *

__all__ = [
    "get_db",
    "init_db",
    "normalize_words",
    "consume_daily_quota",
    "validate_tts_params",
    "VOICE_PATTERN",
    "_migrate_words_table",
    "upsert_feedback",
    "get_feedback_stats",
    "collect_platform_dashboard",
    "list_platform_history",
    "get_setting",
    "set_setting",
    "record_model_usage",
    "get_model_usage_stats",
    "collect_platform_usage",
    "inherit_link_frequency",
    "current_uid",
    "current_user",
    "UserLogFilter",
    "setup_stream_logger",
    "_user_db_path",
    "ensure_db_initialized",
    "AI_WORD_BLACKLIST",
    "is_clean_ai_word",
    "clean_meaning_residue",
]

# ========================================================================
# 用户级数据库隔离
# ========================================================================

# 当前请求所属用户 uid（由认证依赖写入；每请求一个上下文，天然线程/协程安全）
current_uid: ContextVar = ContextVar("current_uid", default=None)

# 当前请求所属用户完整信息（uid/username/role，由认证依赖写入；供日志过滤器注入用户身份）
current_user: ContextVar = ContextVar("current_user", default=None)


class UserLogFilter(logging.Filter):
    """把当前请求用户（uid/username/role）注入每条 wordisle.* / uvicorn.access 日志记录。

    让后台日志能回答「谁调用了什么模型、做了什么」；无用户上下文（启动 / 健康检查等）
    时回退为 '-'，保证任何记录都带 user 字段、格式化不报错。
    """

    def filter(self, record):
        user = current_user.get(None)
        if user:
            record.uid = user.get("uid") or "-"
            record.username = user.get("username") or record.uid
            record.role = user.get("role") or "-"
        else:
            uid = current_uid.get(None)
            record.uid = uid or "-"
            record.username = record.uid
            record.role = "-"
        return True


def setup_stream_logger(name: str) -> logging.Logger:
    """创建带用户身份注入的控制台 logger（stdout / systemd journal 可见 user=...）。

    services / routes / auth 共用同一套格式，保证后台日志每一行都带上
    user=用户名(uid=xx role=xx)，便于追查「谁调用了什么模型、做了什么」。
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter(
            "%(levelname)s [%(name)s] user=%(username)s(uid=%(uid)s role=%(role)s) %(message)s"
        ))
        _h.addFilter(UserLogFilter())
        logger.addHandler(_h)
        logger.setLevel(logging.INFO)
    return logger

# 已初始化过的用户库（进程内缓存，避免每请求重跑 init_db）
_initialized_dbs: set[str] = set()


def _user_db_path(uid: str):
    """返回某 uid 的业务库路径。开发者（dev 角色）固定 dev-wordisle.db，
    其余（管理员/游客）按 uid 独立文件，天然完全隔离。"""
    name = "dev-wordisle.db" if uid == DEV_USERNAME else f"{uid}.db"
    return USER_DATA_DIR / name


def ensure_db_initialized(uid: str):
    """确保某用户库的表结构已初始化（首次访问时执行一次）。"""
    if uid in _initialized_dbs:
        return
    init_db(uid)
    _initialized_dbs.add(uid)

# ========================================================================
# SQLite 数据库
# ========================================================================

def get_db(uid=None) -> sqlite3.Connection:
    """打开某用户的业务库连接。uid 缺省时取当前请求上下文（认证依赖写入）。
    业务 SQL 零改动：全部端点照常调用 get_db()，自动落到对应用户的库。"""
    uid = uid or current_uid.get(None) or DEV_USERNAME
    conn = sqlite3.connect(str(_user_db_path(uid)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_words_table(conn):
    """将旧版 words 表（多字段）迁移为极简结构 (word, pos, meaning_zh, created_at)。
    如果表不存在或已经是新结构，则跳过。
    """
    # 检查表是否存在
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='words'"
    ).fetchone()
    if row is None:
        return  # 表不存在，后续 CREATE TABLE IF NOT EXISTS 会处理

    # 检查是否已经是新结构（有 pos 列）
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(words)").fetchall()]
    if "pos" in cols:
        return  # 已经是新结构

    # 旧结构 → 新结构：只保留 word 和 created_at
    conn.executescript("""
        CREATE TABLE words_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE,
            pos TEXT DEFAULT '',
            meaning_zh TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO words_new (word, created_at)
            SELECT word, COALESCE(created_at, datetime('now','localtime')) FROM words;
        DROP TABLE words;
        ALTER TABLE words_new RENAME TO words;
    """)


def init_db(uid=None):
    """初始化（或幂等重建）某用户的业务库表结构 + 种子数据。
    uid 缺省时取当前请求上下文（默认开发者）。"""
    conn = get_db(uid)

    # 迁移：旧版 words 表 → 极简结构 (word, pos, meaning_zh, created_at)
    _migrate_words_table(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE,
            pos TEXT DEFAULT '',
            meaning_zh TEXT DEFAULT '',
            phonetic TEXT DEFAULT '',
            audio_url TEXT DEFAULT '',
            frequency_level TEXT DEFAULT '',
            frequency_source TEXT DEFAULT 'llm',
            healed_at TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 构词拆解：词根主档（树的节点）
        CREATE TABLE IF NOT EXISTS word_roots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root TEXT NOT NULL UNIQUE,
            root_zh TEXT DEFAULT '',
            root_type TEXT DEFAULT '',
            sense TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 构词拆解：词 → 结构（树的叶子与关联主档）
        CREATE TABLE IF NOT EXISTS word_structures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE,
            structure_code TEXT DEFAULT '',
            morphemes TEXT DEFAULT '{}',
            word_family TEXT DEFAULT '[]',
            is_decomposable INTEGER DEFAULT 1,
            model TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 构词拆解：词 ↔ 词根 多对多（source=scan 收录 | seed LLM推荐）
        CREATE TABLE IF NOT EXISTS word_root_links (
            word TEXT NOT NULL,
            root_id INTEGER NOT NULL,
            source TEXT DEFAULT 'scan',
            frequency_level TEXT DEFAULT '',
            meaning_zh TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (word, root_id),
            FOREIGN KEY (word) REFERENCES word_structures(word) ON DELETE CASCADE,
            FOREIGN KEY (root_id) REFERENCES word_roots(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY,
            words TEXT NOT NULL,
            panel_count INTEGER DEFAULT 4,
            theme_hint  TEXT DEFAULT '',
            story_title TEXT DEFAULT '',
            theme       TEXT DEFAULT '',
            story_synopsis TEXT DEFAULT '',
            body_en      TEXT DEFAULT '',
            model        TEXT DEFAULT '',
            image_model  TEXT DEFAULT '',
            panels       TEXT DEFAULT '[]',
            polysemy_notes  TEXT DEFAULT '{}',
            included_words  TEXT DEFAULT '[]',
            missing_words   TEXT DEFAULT '[]',
            ending_moral TEXT DEFAULT '',
            is_favorited INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS audios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id TEXT NOT NULL,
            file_name     TEXT NOT NULL,
            voice         TEXT DEFAULT '',
            speed         REAL DEFAULT 1.0,
            tts_model     TEXT DEFAULT '',
            duration_ms   INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (generation_id) REFERENCES generations(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS daily_usage (
            day TEXT PRIMARY KEY,
            ai_count  INTEGER DEFAULT 0,
            tts_count INTEGER DEFAULT 0,
            image_count INTEGER DEFAULT 0
        );
        -- 模型调用明细日志（无上限，供「用量情况」页面展示近期模型调用）
        -- tokens 语义：LLM/TTS 为预估 token 数；图片为生成张数；视频为生成秒数
        CREATE TABLE IF NOT EXISTS model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            detail TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            words TEXT NOT NULL,
            theme_hint TEXT DEFAULT '',
            story_title TEXT DEFAULT '',
            narration_en TEXT DEFAULT '',
            narration_zh TEXT DEFAULT '',
            video_prompt TEXT DEFAULT '',
            script TEXT DEFAULT '{}',
            model TEXT DEFAULT '',
            duration INTEGER DEFAULT 10,
            file_name TEXT DEFAULT '',
            video_url TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS polysemy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE,
            common_meaning_zh TEXT DEFAULT '',
            common_meaning_en TEXT DEFAULT '',
            business_meaning_zh TEXT DEFAULT '',
            business_meaning_en TEXT DEFAULT '',
            example_en TEXT DEFAULT '',
            example_zh TEXT DEFAULT '',
            collocations TEXT DEFAULT '[]',
            toc_part TEXT DEFAULT '',
            frequency_level TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 场景聚汇相关表
        CREATE TABLE IF NOT EXISTS scenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name_en TEXT NOT NULL,
            name_zh TEXT NOT NULL,
            description TEXT DEFAULT '',
            cover_image_url TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS word_scenes (
            word_id INTEGER NOT NULL,
            scene_id INTEGER NOT NULL,
            source TEXT DEFAULT 'detect',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (word_id, scene_id),
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE,
            FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS scene_collocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id INTEGER NOT NULL,
            phrase_en TEXT NOT NULL,
            phrase_zh TEXT DEFAULT '',
            words TEXT DEFAULT '[]',
            example_en TEXT DEFAULT '',
            example_zh TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id TEXT NOT NULL,
            rating TEXT NOT NULL CHECK (rating IN ('up','down')),
            comment TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE (generation_id, rating)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        );
        -- 记忆测试：复习排期（Leitner 固定档位，挂词不挂卡）
        CREATE TABLE IF NOT EXISTS review_schedule (
            word TEXT PRIMARY KEY,
            generation_id TEXT DEFAULT '',
            box INTEGER DEFAULT 0,
            next_review_at TEXT DEFAULT '',
            lapses INTEGER DEFAULT 0,
            correct_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 记忆测试：作答日志（streak 与正确率数据源，只增不改，不加外键）
        CREATE TABLE IF NOT EXISTS review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('correct','wrong')),
            question_type TEXT DEFAULT '',
            answered_at TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 智能助手词小屿：会话消息（按用户隔离，保留最近 N 轮）
        CREATE TABLE IF NOT EXISTS assistant_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,             -- user / assistant
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_conv_user
            ON assistant_conversations(user_id, id);
        -- 智能助手词小屿：单条回答的点赞点踩（重复提交同向 = 取消）
        CREATE TABLE IF NOT EXISTS assistant_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            question TEXT NOT NULL DEFAULT '',
            answer TEXT NOT NULL DEFAULT '',
            rating TEXT NOT NULL CHECK (rating IN ('up','down')),
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_fb_user
            ON assistant_feedback(user_id, id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_audios_unique
            ON audios (generation_id, voice, speed, tts_model);
    """)
    # 迁移：为已有数据库添加 tts_model 列
    try:
        conn.execute("ALTER TABLE audios ADD COLUMN tts_model TEXT DEFAULT ''")
    except Exception:
        pass
    # 迁移：清理 audios 重复行并为已有数据库建立唯一索引（并发合成去重）
    try:
        conn.execute("""
            DELETE FROM audios WHERE id NOT IN (
                SELECT MIN(id) FROM audios GROUP BY generation_id, voice, speed, tts_model
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audios_unique ON audios (generation_id, voice, speed, tts_model)")
        conn.commit()
    except Exception:
        pass
    # 迁移：为已有 generations 表添加新字段（panels 模式）
    for col, decl in [
        ("panel_count", "INTEGER DEFAULT 4"),
        ("theme_hint", "TEXT DEFAULT ''"),
        ("story_title", "TEXT DEFAULT ''"),
        ("theme", "TEXT DEFAULT ''"),
        ("story_synopsis", "TEXT DEFAULT ''"),
        ("image_model", "TEXT DEFAULT ''"),
        ("panels", "TEXT DEFAULT '[]'"),
        ("ending_moral", "TEXT DEFAULT ''"),
        ("generation_type", "TEXT DEFAULT 'batch'"),
        ("style", "TEXT DEFAULT ''"),
        ("video_url", "TEXT DEFAULT ''"),
        ("track", "TEXT DEFAULT 'general'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE generations ADD COLUMN {col} {decl}")
        except Exception:
            pass
    # 迁移：为 daily_usage 添加 image_count 列
    try:
        conn.execute("ALTER TABLE daily_usage ADD COLUMN image_count INTEGER DEFAULT 0")
    except Exception:
        pass
    # 迁移：为 model_usage 添加 tokens 列（LLM/TTS 预估 token，图=张数，视频=秒数）
    try:
        conn.execute("ALTER TABLE model_usage ADD COLUMN tokens INTEGER DEFAULT 0")
    except Exception:
        pass
    # 迁移：为 word_scenes 添加 source 列（detect/adopt/manual），用于一词可入多场景时按来源清理
    try:
        conn.execute("ALTER TABLE word_scenes ADD COLUMN source TEXT DEFAULT 'detect'")
    except Exception:
        pass
    # 迁移：words 表追加全局频率列（单词级单一事实来源，三页同源）
    try:
        conn.execute("ALTER TABLE words ADD COLUMN frequency_level TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE words ADD COLUMN frequency_source TEXT DEFAULT 'llm'")
    except Exception:
        pass
    # 迁移：为 words 添加 phonetic 列（音标）
    try:
        conn.execute("ALTER TABLE words ADD COLUMN phonetic TEXT DEFAULT ''")
    except Exception:
        pass
    # 迁移：为 words 添加 audio_url 列（单词发音缓存）
    try:
        conn.execute("ALTER TABLE words ADD COLUMN audio_url TEXT DEFAULT ''")
    except Exception:
        pass
    # 迁移：为 words 添加 healed_at 列（顽固词治愈自评时间，空 = 疗养中）
    try:
        conn.execute("ALTER TABLE words ADD COLUMN healed_at TEXT DEFAULT ''")
    except Exception:
        pass
    # 迁移：清理孤儿复习排期（词已删除但 schedule 残留，会虚增复习统计）
    try:
        conn.execute("DELETE FROM review_schedule WHERE word NOT IN (SELECT word FROM words)")
    except Exception:
        pass
    # 迁移：已入库的熟词僻意频率一次性并入 words（仅在 words 频率为空时补齐）
    _migrate_words_frequency(conn)
    # 迁移：为 feedback 表添加 comment 字段（用户改进意见）
    try:
        conn.execute("ALTER TABLE feedback ADD COLUMN comment TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()


def _migrate_words_frequency(conn):
    """把 polysemy 表中已有的频率一次性并入 words（words 频率为空时补齐），
    实现「熟词僻意 → 单词库」频率全局统一。来源记 seed（种子人工标注）。"""
    rows = conn.execute(
        """SELECT p.word, p.frequency_level FROM polysemy p
           JOIN words w ON w.word = p.word
           WHERE (w.frequency_level IS NULL OR w.frequency_level = '')
             AND p.frequency_level != ''"""
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE words SET frequency_level = ?, frequency_source = 'seed' WHERE word = ?",
            (r["frequency_level"], r["word"]),
        )


def inherit_link_frequency(conn, word: str):
    """P2 推荐词被导入词库时的「暂存 → 继承」：
    把 word_root_links 暂存的频率并入 words（words 频率为空时），并清理 link 上的暂存字段。
    此后该词频率以 words 为准。"""
    w = (word or "").strip().lower()
    if not w:
        return
    link = conn.execute(
        "SELECT frequency_level, meaning_zh FROM word_root_links WHERE word = ? AND source = 'seed' LIMIT 1",
        (w,),
    ).fetchone()
    if not link or not (link["frequency_level"] or link["meaning_zh"]):
        return
    # 并入 words：仅当 words 缺少释义/频率时采用 link 暂存值
    cur = conn.execute(
        "UPDATE words SET frequency_level = CASE WHEN frequency_level = '' THEN ? ELSE frequency_level END, "
        "meaning_zh = CASE WHEN meaning_zh = '' THEN ? ELSE meaning_zh END, "
        "frequency_source = CASE WHEN frequency_level = '' THEN 'seed' ELSE frequency_source END "
        "WHERE word = ?",
        (link["frequency_level"], link["meaning_zh"], w),
    )
    if cur.rowcount:
        # 清理 link 暂存字段，此后以 words 为准
        conn.execute(
            "UPDATE word_root_links SET frequency_level = '', meaning_zh = '' WHERE word = ? AND source = 'seed'",
            (w,),
        )


# ========================================================================
# 高频熟词僻意（含商务义）种子数据


def normalize_words(raw: str) -> list[str]:
    """清洗输入：去标点、去空白、去重"""
    parts = re.split(r"[,\n，]+", raw)
    seen = set()
    clean = []
    for p in parts:
        w = re.sub(r"[^a-zA-Z\-']", "", p.strip()).lower()
        if w and w not in seen and len(w) >= 2:
            seen.add(w)
            clean.append(w)
    return clean


# ========================================================================
# 数据治理：LLM 输出 → 入库前的启发式校验（防测试残留再污染）
# ========================================================================

# 已识别的测试残留词（本库与服务器库体检后确认，黑名单过滤）
AI_WORD_BLACKLIST = {"inplicate", "indegestion", "deprecated", "meeting"}

# 释义中的会话残留元信息（"技术语境…" 这类 AI 自我注释，非真实释义）
MEANING_RESIDUE_MARKERS = ("技术语境", "测试录入", "测试残留", "测试会话", "测试数据", "测试键入")


def is_clean_ai_word(w: str) -> bool:
    """校验 LLM 输出的候选词是否可信（入库防线）：
    纯英文 + 长度合理 + 非黑名单 + 无明显乱码。返回 False 则拒绝入库。"""
    w = (w or "").strip().lower()
    if not w or len(w) < 2 or len(w) > 40:
        return False
    if not re.fullmatch(r"[a-z][a-z'\-]*", w):
        return False
    if w in AI_WORD_BLACKLIST:
        return False
    # 连续 3+ 个相同字母（如 "llllll" 类乱码）
    if re.search(r"(.)\1{2,}", w):
        return False
    return True


def clean_meaning_residue(meaning: str) -> str:
    """剥离释义中的会话残留元信息（如"（技术语境中常指…）"），保留真实释义。
    命中残留标记时删除所在括号组；无残留则原样返回。"""
    if not meaning:
        return meaning
    if not any(m in meaning for m in MEANING_RESIDUE_MARKERS):
        return meaning
    # 删除包含残留标记的括号注释（中英文括号均支持）
    cleaned = re.sub(r"\s*[（(][^（）()]*?(?:%s)[^（）()]*?[)）]\s*" % "|".join(MEANING_RESIDUE_MARKERS), "", meaning)
    return cleaned.strip().rstrip("；;，,").strip()


def consume_daily_quota(category: str, count: int = 1) -> bool:
    """记录一次当日用量（不再设上限，始终返回 True，仅用于用量统计）。
    使用单条 UPSERT 原子累加，避免显式 BEGIN IMMEDIATE 造成锁冲突。"""
    today = date.today().isoformat()
    col_map = {"ai": "ai_count", "tts": "tts_count", "image": "image_count"}
    col = col_map.get(category, "ai_count")
    conn = get_db()
    try:
        conn.execute(
            f"INSERT INTO daily_usage (day, {col}) VALUES (?, ?) "
            f"ON CONFLICT(day) DO UPDATE SET {col} = {col} + excluded.{col}",
            (today, count),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def record_model_usage(category: str, model: str, detail: str = "", tokens: int = 0):
    """记录一次模型调用明细（category: llm/tts/image/video，无上限）。"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO model_usage (category, model, detail, tokens) VALUES (?,?,?,?)",
            (category, model, detail, tokens),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_model_usage_stats(days: int = 0) -> dict:
    """返回模型调用统计：按日汇总、按模型汇总、最近明细、总量汇总。
    days>0 仅统计近 days 天；days<=0 统计全部历史。"""
    conn = get_db()
    try:
        where, params = "", ()
        if days and days > 0:
            where = "WHERE created_at >= date('now','localtime',?)"
            params = (f"-{days} days",)
        calendar = [
            dict(r) for r in conn.execute(
                f"""SELECT substr(created_at,1,10) AS day, category, COUNT(*) AS cnt
                    FROM model_usage
                    {where}
                    GROUP BY day, category ORDER BY day, category""",
                params,
            ).fetchall()
        ]
        models = [
            dict(r) for r in conn.execute(
                f"""SELECT category, model, COUNT(*) AS cnt, SUM(tokens) AS tokens
                    FROM model_usage
                    {where}
                    GROUP BY category, model ORDER BY cnt DESC, model""",
                params,
            ).fetchall()
        ]
        recent = [
            dict(r) for r in conn.execute(
                "SELECT id, category, model, detail, tokens, created_at FROM model_usage ORDER BY id DESC LIMIT 100",
            ).fetchall()
        ]
        # 总量汇总（calls=调用次数，tokens=用量：LLM/TTS 为预估 token，图=张数，视频=秒数）
        summary = {"calls": {"llm": 0, "tts": 0, "image": 0, "video": 0},
                   "tokens": {"llm": 0, "tts": 0, "image": 0, "video": 0}}
        for m in models:
            summary["calls"][m["category"]] = summary["calls"].get(m["category"], 0) + m["cnt"]
            summary["tokens"][m["category"]] = summary["tokens"].get(m["category"], 0) + (m["tokens"] or 0)
        return {"calendar": calendar, "models": models, "recent": recent, "summary": summary}
    finally:
        conn.close()


def collect_platform_usage(days: int = 0) -> dict:
    """跨库聚合全部用户的模型调用统计（开发者/管理员用量页数据源）。

    在 get_model_usage_stats 的结构基础上额外提供：
      - recent 每行追加 username/role（谁调用了什么模型做了什么）
      - users_rank：用户用量排行榜（按调用次数降序，含各分类调用量与最近活跃）
    days>0 仅统计近 days 天；days<=0 统计全部历史。
    """
    users = _iter_user_profiles()
    if not users:
        users = [{"uid": DEV_USERNAME, "username": DEV_USERNAME, "role": "dev", "created_at": ""}]

    where_sql, params = "", ()
    if days and days > 0:
        where_sql = "WHERE created_at >= date('now','localtime',?)"
        params = (f"-{days} days",)

    cal_map: dict[tuple, int] = {}
    mod_map: dict[tuple, dict] = {}   # (category, model) -> {cnt, tokens}
    recent_all: list[dict] = []
    rank_map: dict[str, dict] = {}    # uid -> 排行行

    for u in users:
        uid, uname, role = u["uid"], u["username"], u["role"]
        path = USER_DATA_DIR / (f"{uid}.db" if uid != DEV_USERNAME else "dev-wordisle.db")
        if not path.exists():
            continue
        try:
            conn = _open_user_db_readonly(path)
        except Exception:
            continue
        try:
            # 每日 × 类别 汇总
            rows = conn.execute(
                f"""SELECT substr(created_at,1,10) AS day, category, COUNT(*) AS cnt
                    FROM model_usage {where_sql} GROUP BY day, category""",
                params,
            ).fetchall()
            for r in rows:
                key = (r["day"], r["category"])
                cal_map[key] = cal_map.get(key, 0) + r["cnt"]
            # 模型 × 类别 汇总
            rows = conn.execute(
                f"""SELECT category, model, COUNT(*) AS cnt, SUM(tokens) AS tokens
                    FROM model_usage {where_sql} GROUP BY category, model""",
                params,
            ).fetchall()
            for r in rows:
                key = (r["category"], r["model"])
                m = mod_map.setdefault(key, {"cnt": 0, "tokens": 0})
                m["cnt"] += r["cnt"]
                m["tokens"] += (r["tokens"] or 0)
            # 最近明细（每库取最近 60 条，跨库合并后统一排序截断）
            recent_rows = conn.execute(
                "SELECT id, category, model, detail, tokens, created_at "
                "FROM model_usage ORDER BY id DESC LIMIT 60"
            ).fetchall()
            for r in recent_rows:
                recent_all.append({
                    "key": f"{uid}:{r['id']}",  # 每库 id 各自自增，加 uid 前缀保证全局唯一
                    "category": r["category"], "model": r["model"],
                    "detail": r["detail"] or "", "tokens": r["tokens"] or 0,
                    "created_at": r["created_at"],
                    "username": uname, "role": role,
                })
            # 用户排行（不受 days 窗口限制，反映全部使用情况）
            rank_rows = conn.execute(
                """SELECT category, COUNT(*) AS cnt, SUM(tokens) AS tokens,
                          MAX(created_at) AS last_active
                   FROM model_usage GROUP BY category"""
            ).fetchall()
            entry = rank_map.setdefault(uid, {
                "uid": uid, "username": uname, "role": role,
                "calls": 0, "per": {}, "last_active": None,
                "last_detail": "",
            })
            for rr in rank_rows:
                cat = rr["category"]
                per = entry["per"].setdefault(cat, {"calls": 0, "tokens": 0})
                per["calls"] += rr["cnt"]
                per["tokens"] += (rr["tokens"] or 0)
                entry["calls"] += rr["cnt"]
                if rr["last_active"] and rr["last_active"] > (entry["last_active"] or ""):
                    entry["last_active"] = rr["last_active"]
            # 最近一次调用说明（模型 + 功能，供排行榜展示）
            last = conn.execute(
                "SELECT detail, category, model FROM model_usage ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if last:
                entry["last_detail"] = f"{last['model']} · {(last['detail'] or last['category'] or '')}"
        finally:
            conn.close()

    # 汇总输出
    calendar = [{"day": d, "category": c, "cnt": n}
                for (d, c), n in sorted(cal_map.items(), key=lambda kv: (kv[0][0], kv[0][1]))]
    models = [
        {"category": c, "model": m, "cnt": v["cnt"], "tokens": v["tokens"]}
        for (c, m), v in sorted(mod_map.items(), key=lambda kv: -kv[1]["cnt"])
    ]
    recent_all.sort(key=lambda r: r["created_at"], reverse=True)
    recent = recent_all[:100]

    summary = {"calls": {"llm": 0, "tts": 0, "image": 0, "video": 0},
               "tokens": {"llm": 0, "tts": 0, "image": 0, "video": 0}}
    for m in models:
        summary["calls"][m["category"]] = summary["calls"].get(m["category"], 0) + m["cnt"]
        summary["tokens"][m["category"]] = summary["tokens"].get(m["category"], 0) + (m["tokens"] or 0)

    rank_list = sorted(rank_map.values(), key=lambda r: (-r["calls"], r["username"]))
    return {"calendar": calendar, "models": models, "recent": recent,
            "summary": summary, "users_rank": rank_list}


def upsert_feedback(generation_id: str, rating: str, comment: str = "") -> dict:
    """记录用户对某条生成结果的反馈（up/down）。
    同一 generation 的同一 rating 幂等：重复点击相同值即取消反馈。
    comment 可选，用于记录用户的改进意见。
    返回 {generation_id, rating, comment} 或 None（取消时）。"""
    if rating not in ("up", "down"):
        raise HTTPException(400, "feedback rating 只能是 up 或 down")
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM feedback WHERE generation_id=? AND rating=?",
            (generation_id, rating),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM feedback WHERE generation_id=? AND rating=?",
                (generation_id, rating),
            )
            conn.commit()
            return None
        conn.execute(
            "INSERT OR IGNORE INTO feedback (generation_id, rating, comment) VALUES (?,?,?)",
            (generation_id, rating, comment),
        )
        conn.commit()
        return {"generation_id": generation_id, "rating": rating, "comment": comment}
    finally:
        conn.close()


def get_feedback_stats() -> dict:
    """聚合反馈满意度统计。"""
    conn = get_db()
    try:
        up = conn.execute("SELECT COUNT(*) c FROM feedback WHERE rating='up'").fetchone()["c"]
        down = conn.execute("SELECT COUNT(*) c FROM feedback WHERE rating='down'").fetchone()["c"]
        rated = conn.execute("SELECT COUNT(DISTINCT generation_id) c FROM feedback").fetchone()["c"]
        total = up + down
        return {
            "up": up,
            "down": down,
            "rated": rated,
            "total": total,
            "satisfaction": round(up / total, 4) if total else 0.0,
        }
    finally:
        conn.close()


def upsert_assistant_feedback(user: str, question: str, answer: str, rating: str) -> dict | None:
    """记录用户对词小屿某条回答的点赞/点踩（up/down）。
    同一 (用户, 问题, 回答) 的同一 rating 已存在则视为取消（删除该条），返回 None；
    否则入库并返回写入记录。换方向（up↔down）不互斥，各自独立 toggle。"""
    if rating not in ("up", "down"):
        raise HTTPException(400, "feedback rating 只能是 up 或 down")
    conn = get_db(user)
    try:
        existing = conn.execute(
            "SELECT id FROM assistant_feedback WHERE user_id=? AND question=? AND answer=? AND rating=?",
            (user, question, answer, rating),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM assistant_feedback WHERE user_id=? AND question=? AND answer=? AND rating=?",
                (user, question, answer, rating),
            )
            conn.commit()
            return None
        conn.execute(
            "INSERT INTO assistant_feedback (user_id, question, answer, rating) VALUES (?,?,?,?)",
            (user, question, answer, rating),
        )
        conn.commit()
        return {"user_id": user, "question": question, "answer": answer, "rating": rating}
    finally:
        conn.close()


# ========================================================================
# 平台看板：跨用户库聚合（仅开发者/管理员可用的运维视图）
# ========================================================================

def _iter_user_profiles() -> list[dict]:
    """从全局库返回全部用户档案 [{uid, username, role, created_at}]。"""
    conn = sqlite3.connect(str(SYSTEM_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT uid, username, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _open_user_db_readonly(path) -> sqlite3.Connection:
    """只读打开某用户的业务库（不触发建表/不写 WAL），防止聚合干扰线上数据。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_feedback_comment(path):
    """存量库兜底迁移：为 feedback 表补 comment 列（用户改进意见）。

    运营看板会扫描所有用户库，而旧库（新版代码部署后未再登录过、未触发
    init_db 迁移）可能缺该列，导致聚合查询报 no such column。此函数幂等，
    迁移失败（列已存在 / 表缺失）时静默跳过，不影响线上数据。
    """
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute("ALTER TABLE feedback ADD COLUMN comment TEXT DEFAULT ''")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# 生成类型 → 中文标签（与前端 generationTypeLabel 对齐）
GENERATION_TYPE_LABELS = {
    "single": "单点深耕",
    "batch": "批量编译",
    "scene": "场景编译",
    "video": "视频编译",
}


def collect_platform_dashboard(days: int = 30) -> dict:
    """跨库聚合平台级运维视图（反馈看板数据源）。

    遍历系统库中的全部用户，只读扫描各自的业务库，聚合：
      - 全局概览：用户数 / 活跃用户 / 累计生成 / 反馈满意度
      - 近 N 天活跃度序列（每日生成数 / 反馈数 / 活跃用户数）
      - 生成类型分布
      - 每个用户的统计（生成量 / 反馈量 / 满意度 / 最近活跃）
      - 最近反馈明细（含生成标题与用户身份）
    days 取值范围 [7, 90]，用于活跃度窗口。
    """
    days = max(7, min(int(days) if str(days).isdigit() else 30, 90))
    today = date.today()
    start = today - timedelta(days=days - 1)
    start_str = start.isoformat()

    users = _iter_user_profiles()
    # 无 system.db 用户时退化为纯汇总（防御：个别环境未播种）
    if not users:
        users = [{"uid": DEV_USERNAME, "username": DEV_USERNAME, "role": "dev", "created_at": ""}]

    day_index = [(start + timedelta(days=i)) for i in range(days)]
    day_strs = [d.isoformat() for d in day_index]
    # 活跃度：day -> {generations, feedback, active_uids:set}
    activity = {d: {"generations": 0, "feedback": 0, "active_uids": set()} for d in day_strs}
    del day_index  # 后续只用字符串日期，避免与 SQL date() 返回类型混淆

    user_rows: list[dict] = []       # 每用户统计（按生成量降序）
    recent_feedback: list[dict] = [] # 最近反馈（跨库合并后排序分页）
    type_dist: dict[str, int] = {}   # generation_type -> cnt
    gen_total = audios_total = videos_total = 0
    fb_up_total = fb_down_total = fb_rated_total = 0

    for u in users:
        uid, uname, role = u["uid"], u["username"], u["role"]
        path = USER_DATA_DIR / (f"{uid}.db" if uid != DEV_USERNAME else "dev-wordisle.db")
        if not path.exists():
            # 系统库有记录但业务库未初始化（正常情况极少见），按零统计兜底
            user_rows.append({
                "uid": uid, "username": uname, "role": role,
                "generations": 0, "audios": 0, "videos": 0,
                "feedback_up": 0, "feedback_down": 0, "feedback_rated": 0,
                "satisfaction": 0.0, "last_active": None, "created_at": u.get("created_at") or "",
            })
            continue
        # 存量库兜底：确保 feedback 表含 comment 列（旧库可能未迁移）
        _ensure_feedback_comment(path)
        try:
            conn = _open_user_db_readonly(path)
        except Exception:
            continue  # 文件损坏等：跳过该用户，不影响整体看板
        try:
            # —— 生成记录（总数 / 最长活跃 / 每日生成 / 类型分布）——
            row = conn.execute(
                "SELECT COUNT(*) c, COALESCE(MAX(created_at),'') m FROM generations"
            ).fetchone()
            g_count, last_gen = row["c"], row["m"] or ""
            gen_total += g_count
            type_rows = conn.execute(
                "SELECT generation_type t, COUNT(*) c FROM generations GROUP BY generation_type"
            ).fetchall()
            for tr in type_rows:
                t = tr["t"] or "batch"
                type_dist[t] = type_dist.get(t, 0) + tr["c"]
            day_rows = conn.execute(
                "SELECT date(created_at) d, COUNT(*) c FROM generations "
                "WHERE created_at >= ? GROUP BY d", (start_str,),
            ).fetchall()
            for dr in day_rows:
                if (a := activity.get(dr["d"])) is not None:
                    a["generations"] += dr["c"]
                    a["active_uids"].add(uid)
            last_activity = last_gen

            # —— 记忆测试作答日志（活跃信号之一）——
            rl = conn.execute("SELECT MAX(answered_at) m FROM review_log").fetchone()["m"]
            if rl and rl > last_activity:
                last_activity = rl

            # —— 模型调用（活跃信号 + 最近活跃）——
            mu = conn.execute("SELECT MAX(created_at) m FROM model_usage").fetchone()["m"]
            if mu and mu > last_activity:
                last_activity = mu

            # —— 反馈（汇总 / 每日 / 明细）——
            fb_up = conn.execute("SELECT COUNT(*) c FROM feedback WHERE rating='up'").fetchone()["c"]
            fb_down = conn.execute("SELECT COUNT(*) c FROM feedback WHERE rating='down'").fetchone()["c"]
            fb_rated = conn.execute("SELECT COUNT(DISTINCT generation_id) c FROM feedback").fetchone()["c"]
            fb_up_total += fb_up
            fb_down_total += fb_down
            fb_rated_total += fb_rated
            fb_day_rows = conn.execute(
                "SELECT date(created_at) d, COUNT(*) c FROM feedback "
                "WHERE created_at >= ? GROUP BY d", (start_str,),
            ).fetchall()
            for fdr in fb_day_rows:
                d = fdr["d"]
                if (a := activity.get(d)) is not None:
                    a["feedback"] += fdr["c"]
                    a["active_uids"].add(uid)
            if fb_day_rows and (fdr_max := max(r["d"] for r in fb_day_rows)) > last_activity:
                last_activity = fdr_max + " 00:00:00"
            recent_rows = conn.execute(
                "SELECT f.id, f.generation_id, f.rating, f.comment, f.created_at, "
                "       g.story_title, g.generation_type "
                "FROM feedback f LEFT JOIN generations g ON g.id = f.generation_id "
                "ORDER BY f.created_at DESC LIMIT 50"
            ).fetchall()
            for r in recent_rows:
                recent_feedback.append({
                    "generation_id": r["generation_id"],
                    "title": r["story_title"] or "",
                    "generation_type": r["generation_type"] or "batch",
                    "username": uname, "role": role,
                    "rating": r["rating"], "comment": r["comment"] or "",
                    "created_at": r["created_at"],
                })

            # —— 音频 / 视频数量 ——
            aud = conn.execute("SELECT COUNT(*) c FROM audios").fetchone()["c"]
            vids = conn.execute("SELECT COUNT(*) c FROM videos").fetchone()["c"]
            audios_total += aud
            videos_total += vids

            fbtotal = fb_up + fb_down
            user_rows.append({
                "uid": uid, "username": uname, "role": role,
                "generations": g_count, "audios": aud, "videos": vids,
                "feedback_up": fb_up, "feedback_down": fb_down, "feedback_rated": fb_rated,
                "satisfaction": round(fb_up / fbtotal, 4) if fbtotal else 0.0,
                "last_active": last_activity or None, "created_at": u.get("created_at") or "",
            })
        finally:
            conn.close()

    # —— 活跃用户窗口（今日 / 近 7 天 / 近 N 天）——
    def _active_users(days_back: int) -> int:
        _set = set()
        for _d in day_strs:
            if _d >= (today - timedelta(days=days_back - 1)).isoformat():
                _set |= activity[_d]["active_uids"]
        return len(_set)

    # —— 最近反馈：跨库合并后取最新 20 条 ——
    recent_feedback.sort(key=lambda x: x["created_at"], reverse=True)
    recent_feedback = recent_feedback[:20]

    # —— 类型分布 ——
    type_dist_list = [
        {"type": t, "label": GENERATION_TYPE_LABELS.get(t, t), "cnt": c}
        for t, c in sorted(type_dist.items(), key=lambda kv: -kv[1])
    ]

    user_rows.sort(key=lambda r: (-r["generations"], r["username"]))

    fb_total = fb_up_total + fb_down_total
    return {
        "days": days,
        "stats": {
            "users_total": len(users),
            "users_active_1d": _active_users(1),
            "users_active_7d": _active_users(7),
            "users_active_days": _active_users(days),
            "generations_total": gen_total,
            "audios_total": audios_total,
            "videos_total": videos_total,
            "feedback": {
                "up": fb_up_total, "down": fb_down_total,
                "rated": fb_rated_total, "total": fb_total,
                "satisfaction": round(fb_up_total / fb_total, 4) if fb_total else 0.0,
            },
        },
        "activity": [
            {"day": d, "generations": activity[d]["generations"],
             "feedback": activity[d]["feedback"],
             "active_users": len(activity[d]["active_uids"])}
            for d in day_strs
        ],
        "type_dist": type_dist_list,
        "users": user_rows,
        "recent_feedback": recent_feedback,
    }


def list_platform_history(page: int = 1, page_size: int = 20, q: str = "",
                          rating: str = "", role: str = "") -> dict:
    """跨库聚合全部用户的生成历史（按生成时间倒序，分页 + 过滤）。

    q      —— 搜索标题 / 剧情简介 / 单词（不区分大小写）
    rating —— 'up' / 'down'：只返回有该反馈的记录；'none'：无反馈；空：全部
    role   —— dev / admin / guest：只返回该角色用户的记录；空：全部
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size) or 20, 100))
    q = (q or "").strip().lower()
    rating = (rating or "").strip().lower()
    role = (role or "").strip().lower()
    users = _iter_user_profiles()
    if not users:
        users = [{"uid": DEV_USERNAME, "username": DEV_USERNAME, "role": "dev", "created_at": ""}]

    rows: list[dict] = []
    for u in users:
        uid, uname, urole = u["uid"], u["username"], u["role"]
        if role and urole != role:
            continue
        path = USER_DATA_DIR / (f"{uid}.db" if uid != DEV_USERNAME else "dev-wordisle.db")
        if not path.exists():
            continue
        try:
            conn = _open_user_db_readonly(path)
        except Exception:
            continue
        try:
            recs = conn.execute("""
                SELECT g.id, g.generation_type, g.style, g.story_title, g.theme,
                       g.story_synopsis, g.body_en, g.words, g.panel_count,
                       g.is_favorited, g.created_at, g.included_words, g.missing_words,
                       g.video_url, g.panels,
                       (SELECT COUNT(*) FROM feedback f WHERE f.generation_id=g.id AND f.rating='up') AS fb_up,
                       (SELECT COUNT(*) FROM feedback f WHERE f.generation_id=g.id AND f.rating='down') AS fb_down
                FROM generations g
            """).fetchall()
        finally:
            conn.close()
        for r in recs:
            t = (r["story_title"] or "") + " " + (r["story_synopsis"] or "") + " " + (r["body_en"] or "") + " " + " ".join(json.loads(r["words"] or "[]"))
            if q and q not in t.lower():
                continue
            if rating == "up" and not r["fb_up"]:
                continue
            if rating == "down" and not r["fb_down"]:
                continue
            if rating == "none" and (r["fb_up"] or r["fb_down"]):
                continue
            rows.append({
                "id": r["id"],
                "generation_type": r["generation_type"] or "batch",
                "style": r["style"] or "",
                "story_title": r["story_title"] or "",
                "theme": r["theme"] or "",
                "story_synopsis": r["story_synopsis"] or "",
                "words": json.loads(r["words"] or "[]"),
                "panel_count": r["panel_count"],
                "created_at": r["created_at"],
                "is_favorited": bool(r["is_favorited"]),
                "username": uname, "role": urole,
                "fb_up": r["fb_up"], "fb_down": r["fb_down"],
                "has_feedback": bool(r["fb_up"] or r["fb_down"]),
            })

    rows.sort(key=lambda r: r["created_at"], reverse=True)
    total = len(rows)
    start = (page - 1) * page_size
    return {"total": total, "rows": rows[start:start + page_size]}


def get_setting(key: str, default: str = "") -> str:
    """从 settings 表读取设置值。若表不存在（DB 未初始化），返回 default。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default
    finally:
        conn.close()


def set_setting(key: str, value: str):
    """写入设置值（INSERT OR REPLACE）。"""
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
        conn.commit()
    finally:
        conn.close()


VOICE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_tts_params(voice, speed):
    """校验 TTS 音色与语速参数，防止路径遍历及非法值。"""
    if not isinstance(voice, str) or not VOICE_PATTERN.match(voice):
        raise HTTPException(400, "voice 只能包含字母、数字、下划线和连字符")
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        raise HTTPException(400, "speed 必须为数字")
    if not (0.5 <= speed <= 2.0):
        raise HTTPException(400, "speed 必须在 0.5 ~ 2.0 之间")
    return voice, speed