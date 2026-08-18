"""
TOEIC MVP 数据库层
==================
SQLite 连接管理、表初始化、单词清洗、每日配额、参数校验。
"""

import json
import re
import sqlite3
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
    "SCENES_SEED",
    "upsert_feedback",
    "get_feedback_stats",
    "get_setting",
    "set_setting",
    "record_model_usage",
    "get_model_usage_stats",
    "inherit_link_frequency",
]

# ========================================================================
# SQLite 数据库
# ========================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
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


def init_db():
    conn = get_db()

    # 迁移：旧版 words 表 → 极简结构 (word, pos, meaning_zh, created_at)
    _migrate_words_table(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE,
            pos TEXT DEFAULT '',
            meaning_zh TEXT DEFAULT '',
            frequency_level TEXT DEFAULT '',
            frequency_source TEXT DEFAULT 'llm',
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
    # 迁移：已入库的熟词僻意种子频率一次性并入 words（仅在 words 频率为空时补齐）
    _migrate_words_frequency(conn)
    # 种子数据：托业高频熟词僻意（仅当表为空时插入）
    _seed_polysemy(conn)
    # 种子数据：场景聚汇预设场景（仅当 scenes 表为空时插入）
    _seed_scenes(conn)
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
# ========================================================================

POLYSEMY_SEED = [
    {
        "word": "address",
        "common_meaning_zh": "地址",
        "common_meaning_en": "a location where a person or organization can be found",
        "business_meaning_zh": "处理，解决（问题）；向…发表演说",
        "business_meaning_en": "to deal with a problem or situation; to make a formal speech to an audience",
        "example_en": "We need to address the customer complaints before the meeting.",
        "example_zh": "我们需要在会议前处理客户投诉。",
        "collocations": ["address an issue", "address a problem", "address a meeting", "address concerns"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★★",
    },
    {
        "word": "accommodate",
        "common_meaning_zh": "容纳，接待",
        "common_meaning_en": "to provide housing or space for someone",
        "business_meaning_zh": "满足（需求）；顾及；适应",
        "business_meaning_en": "to provide what is needed or wanted; to adapt to circumstances",
        "example_en": "The hotel can accommodate up to 500 guests for the conference.",
        "example_zh": "这家酒店可容纳最多500位参会客人。",
        "collocations": ["accommodate needs", "accommodate requests", "accommodate changes", "accommodate clients"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "charge",
        "common_meaning_zh": "充电；指控",
        "common_meaning_en": "to fill with energy; to accuse of a crime",
        "business_meaning_zh": "收费；负责；掌管",
        "business_meaning_en": "to ask an amount as a price; to be in charge of; responsible for",
        "example_en": "She is in charge of the marketing department.",
        "example_zh": "她负责市场部。",
        "collocations": ["in charge of", "charge a fee", "free of charge", "take charge"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★★",
    },
    {
        "word": "firm",
        "common_meaning_zh": "坚定的；牢固的",
        "common_meaning_en": "solidly fixed in place; resolute",
        "business_meaning_zh": "公司，商号",
        "business_meaning_en": "a business organization, company",
        "example_en": "She joined a law firm in downtown after graduation.",
        "example_zh": "毕业后她加入了市中心的一家律师事务所。",
        "collocations": ["law firm", "consulting firm", "accounting firm", "firm offer"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "issue",
        "common_meaning_zh": "问题；议题",
        "common_meaning_en": "a problem or topic for discussion",
        "business_meaning_zh": "发行（股票/债券）；发布；签发",
        "business_meaning_en": "to distribute or produce something officially; to publish",
        "example_en": "The company will issue a press release tomorrow morning.",
        "example_zh": "公司将于明天上午发布新闻稿。",
        "collocations": ["issue a statement", "issue shares", "issue an invoice", "press issue"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "order",
        "common_meaning_zh": "顺序；秩序",
        "common_meaning_en": "a particular sequence of things; state of peace",
        "business_meaning_zh": "订单；命令；订购",
        "business_meaning_en": "a request for goods or services; to request products",
        "example_en": "Please confirm your order by the end of the week.",
        "example_zh": "请在本周末前确认您的订单。",
        "collocations": ["place an order", "purchase order", "work order", "order confirmation"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "present",
        "common_meaning_zh": "礼物；现在的",
        "common_meaning_en": "a gift; existing or occurring now",
        "business_meaning_zh": "呈现；提交；出席",
        "business_meaning_en": "to show or offer something; to attend a meeting",
        "example_en": "He will present the quarterly report at the board meeting.",
        "example_zh": "他将在董事会上提交季度报告。",
        "collocations": ["present a report", "present findings", "be present at", "present proposal"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "rate",
        "common_meaning_zh": "比率；速度",
        "common_meaning_en": "a ratio between two measurements; speed",
        "business_meaning_zh": "价格；费率；评价",
        "business_meaning_en": "a fixed price charged per unit; to assess or value",
        "example_en": "The exchange rate for the dollar has dropped significantly.",
        "example_zh": "美元的汇率大幅下跌。",
        "collocations": ["exchange rate", "interest rate", "hourly rate", "flat rate"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★★",
    },
    {
        "word": "share",
        "common_meaning_zh": "分享；分担",
        "common_meaning_en": "to use or enjoy together with others",
        "business_meaning_zh": "股票；股份；市场份额",
        "business_meaning_en": "one of the equal parts into which a company is divided; market share",
        "example_en": "The company's share price rose by 12% last quarter.",
        "example_zh": "该公司股价上季度上涨了12%。",
        "collocations": ["market share", "share price", "ordinary share", "shareholder"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "term",
        "common_meaning_zh": "学期；术语",
        "common_meaning_en": "a period of study at school; a word or expression",
        "business_meaning_zh": "条款；期限；任期",
        "business_meaning_en": "conditions agreed in a contract; a period for which something lasts",
        "example_en": "The terms of the contract are non-negotiable.",
        "example_zh": "合同条款不可协商。",
        "collocations": ["terms and conditions", "long-term", "short-term", "contract terms"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★★",
    },
    {
        "word": "bill",
        "common_meaning_zh": "账单",
        "common_meaning_en": "a statement of money owed for goods or services",
        "business_meaning_zh": "法案；议案；汇票",
        "business_meaning_en": "a draft of a proposed law; a banknote; bill of exchange",
        "example_en": "The senate is voting on the new tax bill tomorrow.",
        "example_zh": "参议院明天将就新税法提案进行投票。",
        "collocations": ["bill of lading", "utility bill", "bill of materials", "pay the bill"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "book",
        "common_meaning_zh": "书籍",
        "common_meaning_en": "a written or printed work consisting of pages",
        "business_meaning_zh": "预订；预约；账面",
        "business_meaning_en": "to reserve accommodation, tickets, etc.; on the books",
        "example_en": "You should book the conference room two weeks in advance.",
        "example_zh": "你应该提前两周预订会议室。",
        "collocations": ["book a room", "book value", "by the book", "cook the books"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "company",
        "common_meaning_zh": "陪伴；交往",
        "common_meaning_en": "the fact or condition of being with another",
        "business_meaning_zh": "公司；企业",
        "business_meaning_en": "a commercial business or enterprise",
        "example_en": "Our company has expanded operations to Southeast Asia.",
        "example_zh": "我们公司已将业务扩展到东南亚。",
        "collocations": ["holding company", "parent company", "limited company", "public company"],
        "toc_part": "Part 5",
        "frequency_level": "★★★★★",
    },
    {
        "word": "course",
        "common_meaning_zh": "课程；课程",
        "common_meaning_en": "a series of educational classes",
        "business_meaning_zh": "过程；航线；一道菜",
        "business_meaning_en": "the way in which something develops; a path of a ship or aircraft",
        "example_en": "We will stay the course despite economic uncertainty.",
        "example_zh": "尽管经济不确定，我们仍将坚持到底。",
        "collocations": ["of course", "in the course of", "main course", "stay the course"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "date",
        "common_meaning_zh": "日期；约会",
        "common_meaning_en": "a specific day; a romantic meeting",
        "business_meaning_zh": "有效期；注明日期",
        "business_meaning_en": "the period a product/document is valid; to mark with a date",
        "example_en": "Please sign and date the contract before submitting it.",
        "example_zh": "请在提交前在合同上签名并注明日期。",
        "collocations": ["due date", "out of date", "up to date", "date of issue"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "draft",
        "common_meaning_zh": "草稿；草图",
        "common_meaning_en": "a preliminary version of a piece of writing",
        "business_meaning_zh": "汇票；征兵；起草",
        "business_meaning_en": "a written order to pay money; to prepare a document",
        "example_en": "I drafted a proposal for the new marketing campaign.",
        "example_zh": "我起草了一份新营销活动的提案。",
        "collocations": ["first draft", "bank draft", "draft a contract", "final draft"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "major",
        "common_meaning_zh": "主要的；较大的",
        "common_meaning_en": "important, serious, or significant",
        "business_meaning_zh": "主修；专业；主修学生",
        "business_meaning_en": "the main subject or course of a student at college",
        "example_en": "She majored in International Business at NYU.",
        "example_zh": "她在纽约大学主修国际商务。",
        "collocations": ["major in", "major player", "major account", "major shareholder"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "note",
        "common_meaning_zh": "笔记；便条",
        "common_meaning_en": "a brief record of facts written down",
        "business_meaning_zh": "票据；纸币；音符",
        "business_meaning_en": "a banknote; a certificate of indebtedness; promissory note",
        "example_en": "Please take notes during the client presentation.",
        "example_zh": "请在客户介绍期间做笔记。",
        "collocations": ["promissory note", "take notes", "credit note", "debit note"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "principal",
        "common_meaning_zh": "主要的；首要的",
        "common_meaning_en": "first in order of importance; main",
        "business_meaning_zh": "本金；委托人；校长",
        "business_meaning_en": "a sum of money lent or invested; a person represented by an agent",
        "example_en": "The principal amount must be repaid within five years.",
        "example_zh": "本金必须在五年内偿还。",
        "collocations": ["principal amount", "principal and interest", "school principal"],
        "toc_part": "Part 6",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "second",
        "common_meaning_zh": "第二；秒",
        "common_meaning_en": "coming after the first; 1/60 of a minute",
        "business_meaning_zh": "支持；附议；临时调任",
        "business_meaning_en": "to officially support a proposal; to transfer to another post",
        "example_en": "I second the motion to approve the budget.",
        "example_zh": "我附议批准预算的动议。",
        "collocations": ["second hand", "second to none", "second a motion"],
        "toc_part": "Part 6",
        "frequency_level": "★★☆☆☆",
    },
    {
        "word": "sound",
        "common_meaning_zh": "声音；听起来",
        "common_meaning_en": "vibrations that travel through air and can be heard",
        "business_meaning_zh": "稳健的；健康的；合理的",
        "business_meaning_en": "financially secure; sensible and reliable",
        "example_en": "The company maintains a sound financial position.",
        "example_zh": "公司保持稳健的财务状况。",
        "collocations": ["sound investment", "sound advice", "financially sound"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "tender",
        "common_meaning_zh": "温柔的；嫩的",
        "common_meaning_en": "showing gentleness, kindness, and affection",
        "business_meaning_zh": "投标；标书；提供",
        "business_meaning_en": "to offer or present something formally; a bid for a contract",
        "example_en": "Three construction companies will tender for the bridge project.",
        "example_zh": "三家建筑公司将为这个桥梁项目投标。",
        "collocations": ["submit a tender", "tender for", "legal tender", "tender offer"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "trust",
        "common_meaning_zh": "信任；相信",
        "common_meaning_en": "firm belief in the reliability or truth of someone",
        "business_meaning_zh": "信托；托拉斯；赊账",
        "business_meaning_en": "a fiduciary relationship; an organization managing property for someone",
        "example_en": "They set up a trust fund for their children's education.",
        "example_zh": "他们为子女的教育设立了信托基金。",
        "collocations": ["trust fund", "unit trust", "mutual trust", "on trust"],
        "toc_part": "Part 6",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "yield",
        "common_meaning_zh": "屈服；让步",
        "common_meaning_en": "to give way to arguments, demands, or pressure",
        "business_meaning_zh": "产量；收益；产出",
        "business_meaning_en": "the amount of agricultural or industrial production; return on investment",
        "example_en": "The high-yield bonds offer attractive returns.",
        "example_zh": "高收益债券提供有吸引力的回报。",
        "collocations": ["high yield", "yield to maturity", "dividend yield", "crop yield"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "account",
        "common_meaning_zh": "账户；说明",
        "common_meaning_en": "a record of financial transactions; a description",
        "business_meaning_zh": "客户；赊账；账户",
        "business_meaning_en": "a business relationship with a client; on account (credit)",
        "example_en": "We landed three major accounts in the first quarter.",
        "example_zh": "我们第一季度拿下了三个主要客户。",
        "collocations": ["key account", "on account", "account for", "account manager"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★★",
    },
    {
        "word": "benefit",
        "common_meaning_zh": "好处；利益",
        "common_meaning_en": "an advantage or profit gained from something",
        "business_meaning_zh": "福利；津贴；保险金",
        "business_meaning_en": "financial or non-financial compensation beyond salary (insurance, leave)",
        "example_en": "Employee benefits include health insurance and paid vacation.",
        "example_zh": "员工福利包括健康保险和带薪假期。",
        "collocations": ["employee benefits", "social benefits", "benefit package", "fringe benefits"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★★",
    },
    {
        "word": "capital",
        "common_meaning_zh": "首都；大写字母",
        "common_meaning_en": "the city where a country's government is based",
        "business_meaning_zh": "资本；资金；资产",
        "business_meaning_en": "money or assets used to start or operate a business",
        "example_en": "The startup raised $5 million in venture capital.",
        "example_zh": "这家初创公司筹集了500万美元的风险投资。",
        "collocations": ["working capital", "venture capital", "capital investment", "capital gain"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "compound",
        "common_meaning_zh": "化合物；复合词",
        "common_meaning_en": "a thing composed of two or more separate elements",
        "business_meaning_zh": "复利；复合增长",
        "business_meaning_en": "interest calculated on both the principal and accumulated interest",
        "example_en": "Compound interest can significantly grow your savings over time.",
        "example_zh": "随着时间推移，复利可以显著增加您的储蓄。",
        "collocations": ["compound interest", "compound growth", "compound annual"],
        "toc_part": "Part 6",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "contract",
        "common_meaning_zh": "合同；契约",
        "common_meaning_en": "a written or spoken agreement that is enforceable by law",
        "business_meaning_zh": "收缩；承包；感染",
        "business_meaning_en": "to decrease in size; to enter into a formal agreement; to acquire",
        "example_en": "Economists expect the GDP to contract 2% this year.",
        "example_zh": "经济学家预计今年GDP将收缩2%。",
        "collocations": ["sign a contract", "breach of contract", "contract out", "contract work"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "credit",
        "common_meaning_zh": "信用；学分",
        "common_meaning_en": "the ability to obtain goods before payment; unit of study",
        "business_meaning_zh": "信贷；贷方；记入贷方",
        "business_meaning_en": "money lent by a bank; the positive side of an account",
        "example_en": "Our company has a $200,000 line of credit with the bank.",
        "example_zh": "我们公司在银行有20万美元的信用额度。",
        "collocations": ["line of credit", "credit rating", "credit limit", "letter of credit"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "current",
        "common_meaning_zh": "当前的；水流；电流",
        "common_meaning_en": "belonging to the present time; a flow of water or electricity",
        "business_meaning_zh": "流动的；经常的；往来账户",
        "business_meaning_en": "ongoing; a current account (checking) for regular transactions",
        "example_en": "Our current assets exceed current liabilities by 3:1.",
        "example_zh": "我们的流动资产是流动负债的3倍。",
        "collocations": ["current account", "current assets", "current ratio", "current liabilities"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "duty",
        "common_meaning_zh": "责任；义务",
        "common_meaning_en": "a moral or legal obligation",
        "business_meaning_zh": "关税；税",
        "business_meaning_en": "a tax on imported or exported goods (customs duty)",
        "example_en": "You may have to pay import duty on these goods.",
        "example_zh": "您可能需要为这些商品缴纳进口关税。",
        "collocations": ["import duty", "customs duty", "duty-free", "stamp duty"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "facility",
        "common_meaning_zh": "设施；设备",
        "common_meaning_en": "a place, amenity, or piece of equipment for a particular purpose",
        "business_meaning_zh": "信贷便利；融资安排",
        "business_meaning_en": "an arrangement with a bank allowing credit or overdraft",
        "example_en": "The bank approved an overdraft facility for our business.",
        "example_zh": "银行为我们的企业批准了透支信贷便利。",
        "collocations": ["credit facility", "overdraft facility", "loan facility", "production facility"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "figure",
        "common_meaning_zh": "数字；人物；身材",
        "common_meaning_en": "a number; a person's bodily shape; a character",
        "business_meaning_zh": "图表；金额；计算",
        "business_meaning_en": "a numerical symbol, especially in statistics; to calculate",
        "example_en": "The sales figures for Q3 exceeded our projections.",
        "example_zh": "第三季度的销售数字超出了我们的预期。",
        "collocations": ["sales figure", "key figures", "figure out", "financial figures"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "gross",
        "common_meaning_zh": "恶心的；总共的",
        "common_meaning_en": "very unpleasant or repulsive; total before deductions",
        "business_meaning_zh": "总额；毛利；毛重",
        "business_meaning_en": "total income, weight, etc. before any deductions (net is after)",
        "example_en": "Gross profit for the year was $2.4 million.",
        "example_zh": "本年度毛利润为240万美元。",
        "collocations": ["gross profit", "gross income", "gross weight", "gross margin"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "inventory",
        "common_meaning_zh": "清单；目录",
        "common_meaning_en": "a complete list of items such as property",
        "business_meaning_zh": "库存；存货；盘点",
        "business_meaning_en": "goods held in stock by a business; stocktaking",
        "example_en": "We maintain a safety inventory of raw materials.",
        "example_zh": "我们维持原材料的安全库存。",
        "collocations": ["inventory control", "take inventory", "safety inventory", "turnover inventory"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "letter",
        "common_meaning_zh": "字母；信件",
        "common_meaning_en": "a character representing a speech sound; a written message",
        "business_meaning_zh": "信函；证书；授权书",
        "business_meaning_en": "formal documents: letter of credit, letter of intent, etc.",
        "example_en": "We received the letter of credit from the importer's bank.",
        "example_zh": "我们收到了进口商银行开立的信用证。",
        "collocations": ["letter of credit", "cover letter", "letter of intent", "demand letter"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "line",
        "common_meaning_zh": "线；线条",
        "common_meaning_en": "a long narrow mark or band",
        "business_meaning_zh": "产品线；额度；排队",
        "business_meaning_en": "range of products; credit limit; assembly line",
        "example_en": "We plan to launch a new product line next spring.",
        "example_zh": "我们计划明年春天推出一条新的产品线。",
        "collocations": ["product line", "line of credit", "bottom line", "assembly line"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★★",
    },
    {
        "word": "margin",
        "common_meaning_zh": "边缘；空白",
        "common_meaning_en": "the edge or border of something; blank space around text",
        "business_meaning_zh": "利润；保证金；差额",
        "business_meaning_en": "profit per unit sold; money deposited as collateral",
        "example_en": "We need to improve our gross margin by cutting costs.",
        "example_zh": "我们需要通过削减成本来提高毛利率。",
        "collocations": ["profit margin", "gross margin", "margin call", "net margin"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "master",
        "common_meaning_zh": "主人；大师",
        "common_meaning_en": "a man who has people working for him; an expert",
        "business_meaning_zh": "硕士；原版；主文件",
        "business_meaning_en": "a master's degree; a master copy; master file/data/schedule",
        "example_en": "Please refer to the master file for the latest inventory data.",
        "example_zh": "请参考主文件获取最新库存数据。",
        "collocations": ["master file", "master plan", "master schedule", "master copy"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "overhead",
        "common_meaning_zh": "在头顶上",
        "common_meaning_en": "above the level of the head; in the sky",
        "business_meaning_zh": "运营费用；管理费用",
        "business_meaning_en": "ongoing business expenses not directly attributed to creating a product",
        "example_en": "We reduced overhead by moving to a smaller office.",
        "example_zh": "我们通过搬到更小的办公室降低了管理费用。",
        "collocations": ["overhead costs", "reduce overhead", "overhead expenses", "administrative overhead"],
        "toc_part": "Part 6",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "premium",
        "common_meaning_zh": "奖品；奖金",
        "common_meaning_en": "a prize or reward given for a specific achievement",
        "business_meaning_zh": "保险费；溢价；附加费",
        "business_meaning_en": "an amount to be paid for an insurance policy; above face value",
        "example_en": "The annual insurance premium is due next month.",
        "example_zh": "年度保险费将于下月到期。",
        "collocations": ["insurance premium", "at a premium", "premium price", "risk premium"],
        "toc_part": "Part 5/6",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "proceeds",
        "common_meaning_zh": "继续；进行（动词）",
        "common_meaning_en": "(verb) begin or continue a course of action",
        "business_meaning_zh": "收入；收益（名词）",
        "business_meaning_en": "(noun) money obtained from an event, sale, or transaction",
        "example_en": "The proceeds from the auction will go to charity.",
        "example_zh": "拍卖所得收益将捐赠给慈善机构。",
        "collocations": ["net proceeds", "gross proceeds", "proceeds of sale", "proceeds from"],
        "toc_part": "Part 6",
        "frequency_level": "★★★☆☆",
    },
    {
        "word": "return",
        "common_meaning_zh": "返回；归还",
        "common_meaning_en": "come or go back to a place or person",
        "business_meaning_zh": "回报；收益；纳税申报",
        "business_meaning_en": "a profit from an investment; tax return; goods returned",
        "example_en": "The fund delivered a 15% return last year.",
        "example_zh": "该基金去年实现了15%的回报。",
        "collocations": ["return on investment", "tax return", "rate of return", "goods return"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "security",
        "common_meaning_zh": "安全；保安",
        "common_meaning_en": "the state of being free from danger or threat",
        "business_meaning_zh": "证券；抵押品；担保",
        "business_meaning_en": "a tradable financial asset; collateral for a loan",
        "example_en": "The bank requires securities as collateral for the loan.",
        "example_zh": "银行要求证券作为贷款的抵押品。",
        "collocations": ["securities market", "social security", "loan security", "security deposit"],
        "toc_part": "Part 6/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "stock",
        "common_meaning_zh": "库存；牲畜",
        "common_meaning_en": "the goods kept in a shop or warehouse; farm animals",
        "business_meaning_zh": "股票；股份；存量",
        "business_meaning_en": "shares in a company; capital raised via shares",
        "example_en": "The tech stock surged 20% after the earnings report.",
        "example_zh": "财报发布后，这只科技股飙升了20%。",
        "collocations": ["stock market", "common stock", "preferred stock", "in stock"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★★",
    },
    {
        "word": "turnover",
        "common_meaning_zh": "翻倒；滚动",
        "common_meaning_en": "the act of turning something over",
        "business_meaning_zh": "营业额；周转率；人员流动",
        "business_meaning_en": "total revenue; rate at which employees leave or stock is sold",
        "example_en": "Staff turnover is high in the retail industry.",
        "example_zh": "零售业人员流动率很高。",
        "collocations": ["annual turnover", "inventory turnover", "staff turnover", "sales turnover"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★☆",
    },
    {
        "word": "venture",
        "common_meaning_zh": "冒险；探险",
        "common_meaning_en": "a risky or daring journey or undertaking",
        "business_meaning_zh": "风险投资；创业项目",
        "business_meaning_en": "a business project or enterprise, typically involving risk; venture capital",
        "example_en": "They formed a joint venture with a Japanese partner.",
        "example_zh": "他们与一家日本合作伙伴成立了合资企业。",
        "collocations": ["joint venture", "venture capital", "venture capitalist", "business venture"],
        "toc_part": "Part 5/7",
        "frequency_level": "★★★★☆",
    },
]


def _seed_polysemy(conn):
    """当 polysemy 表为空时，填入托业高频熟词僻意种子数据。"""
    count = conn.execute("SELECT COUNT(*) FROM polysemy").fetchone()[0]
    if count > 0:
        return
    for item in POLYSEMY_SEED:
        conn.execute(
            """INSERT INTO polysemy
               (word, common_meaning_zh, common_meaning_en,
                business_meaning_zh, business_meaning_en,
                example_en, example_zh, collocations,
                toc_part, frequency_level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                item["word"],
                item["common_meaning_zh"],
                item["common_meaning_en"],
                item["business_meaning_zh"],
                item["business_meaning_en"],
                item["example_en"],
                item["example_zh"],
                json.dumps(item["collocations"], ensure_ascii=False),
                item["toc_part"],
                item["frequency_level"],
            ),
        )


# ========================================================================
# 场景聚汇预设场景种子数据
# ========================================================================

SCENES_SEED = [
    ("HR & Personnel",         "HR/人事",     "招聘、薪酬、福利、合同等人力资源管理"),
    ("Meeting & Events",       "会议/活动",   "会议议程、场地、纪要、休会等"),
    ("Logistics & Procurement", "物流/采购",   "货运、供应商、库存、规格等"),
    ("Finance & Office",       "财务/办公",   "营收、季度、报销、文具等"),
    ("Negotiation & Contract", "谈判/合同",   "投标、合同、谈判、条款等"),
    ("Marketing & Sales",      "营销/销售",   "营销活动、客户、佣金、产品发布等"),
    ("Legal & Compliance",     "法务/合规",   "侵权、责任、合规、监管、专利等"),
    ("Finance & Investment",   "金融/投资",   "股票、股息、收益率、债券、投资组合等"),
]


def _seed_scenes(conn):
    """当 scenes 表为空时，填入预设商务场景。"""
    count = conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
    if count > 0:
        return
    for name_en, name_zh, desc in SCENES_SEED:
        conn.execute(
            "INSERT INTO scenes (name_en, name_zh, description, status) VALUES (?,?,?,?)",
            (name_en, name_zh, desc, "active"),
        )


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