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
]

# ========================================================================
# SQLite 数据库
# ========================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
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
            created_at TEXT DEFAULT (datetime('now','localtime'))
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
    """)
    # 迁移：为已有数据库添加 tts_model 列
    try:
        conn.execute("ALTER TABLE audios ADD COLUMN tts_model TEXT DEFAULT ''")
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
    # 种子数据：托业高频熟词僻意（仅当表为空时插入）
    _seed_polysemy(conn)
    conn.commit()
    conn.close()


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
    """原子地检查并占用一次当日配额（BEGIN IMMEDIATE 事务，超限返回 False）。"""
    today = date.today().isoformat()
    col_map = {"ai": "ai_count", "tts": "tts_count", "image": "image_count"}
    limit_map = {"ai": DAILY_AI_LIMIT, "tts": DAILY_TTS_LIMIT, "image": DAILY_IMAGE_LIMIT}
    col = col_map.get(category, "ai_count")
    limit = limit_map.get(category, DAILY_AI_LIMIT)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM daily_usage WHERE day=?", (today,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO daily_usage(day) VALUES(?)", (today,))
            used = 0
        else:
            used = row[col]
        if used + count > limit:
            conn.execute("ROLLBACK")
            return False
        conn.execute(f"UPDATE daily_usage SET {col}={col}+? WHERE day=?", (count, today))
        conn.execute("COMMIT")
        return True
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