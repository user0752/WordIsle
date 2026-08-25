"""
TOEIC MVP 数据库层
==================
SQLite 连接管理、表初始化、单词清洗、每日配额、参数校验。
"""

import json
import re
import sqlite3
from contextvars import ContextVar
from datetime import date

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
    "get_setting",
    "set_setting",
    "record_model_usage",
    "get_model_usage_stats",
    "inherit_link_frequency",
    "current_uid",
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
# 托业高频熟词僻意种子数据


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


def upsert_feedback(generation_id: str, rating: str) -> dict:
    """记录用户对某条生成结果的反馈（up/down）。
    同一 generation 的同一 rating 幂等：重复点击相同值即取消反馈。
    返回 {generation_id, rating} 或 None（取消时）。"""
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
            "INSERT OR IGNORE INTO feedback (generation_id, rating) VALUES (?,?)",
            (generation_id, rating),
        )
        conn.commit()
        return {"generation_id": generation_id, "rating": rating}
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