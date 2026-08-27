"""
词小屿 —— 词屿全局智能助手（Agent 编排）
==========================================
L0 向导：FAQ 知识库命中直答（零 LLM、零幻觉），附带页面跳转；未命中走 LLM + 诚实兜底。
L1 操作员：Function Calling 意图识别 → 查询工具（查词/查复习）由后端直接执行；
          写操作（加词/删词）只产出「意图」，由前端渲染确认卡片后调真实 API 执行，
          LLM 永不直接执行写操作（安全铁律）。
会话：assistant_conversations 表按用户隔离，上下文取最近 N 轮。
"""

import asyncio
import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from config import BASE_DIR, DEV_USERNAME, REVIEW_DAILY_LIMIT
from db import consume_daily_quota, current_uid, get_db, setup_stream_logger
from services import stream_llm_with_fallback, get_route_llm
from auth import get_current_user, require_quota

logger = setup_stream_logger("wordisle.assistant")

# 所有接口强制登录（复用现有认证依赖，写入 current_uid contextvar）
router = APIRouter(dependencies=[Depends(get_current_user)])


# ========================================================================
# 基础设施：SSE 事件
# ========================================================================

def _sse(event: str, data) -> str:
    # data 必须单行：JSON 里的换行要转义成 \\n，否则 SSE 客户端把它当下一行丢弃（丢文本）
    payload = json.dumps(data, ensure_ascii=False).replace("\n", "\\n").replace("\r", "\\r")
    return f"event: {event}\ndata: {payload}\n\n"


async def _sse_stream(gen):
    """把 (event, data) 生成器转成 SSE 文本流。"""
    async for evt, data in gen:
        yield _sse(evt, data)


async def _stream_text(text: str, chunk: int = 140, gap: float = 0.02):
    """把最终答复文本按行/语义片段逐段 yield `result` 事件，制造流式输出效果。
    切分必须保留原文换行（\n）：块与块之间要补回换行，否则前端拼接后
    变成长行，markdown 的列表/换行语义全部失效。"""
    text = (text or "").strip() or "……"
    parts = _chunk_text(text, chunk)
    n = len(parts)
    for i, p in enumerate(parts):
        yield ("result", {"text": p if i == n - 1 else p + "\n"})
        if gap:
            await asyncio.sleep(gap)


def _chunk_text(text: str, size: int = 140) -> list[str]:
    """按原文换行分块：一行一块；仅当单行超过 size 才在标点处切（切点不额外加换行）。
    这样流式拼接后能还原完整句子与 markdown 结构（列表/加粗不跨行破坏）。"""
    out = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        while len(line) > size:
            cut = -1
            for sep in ("。", "！", "？", "；", ",", "，", " ", ":", "："):
                idx = line.rfind(sep, 0, size + 1)
                if idx > 0:
                    cut = idx + 1
                    break
            if cut <= 0:
                cut = size
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return out


async def _safe_json(req: Request) -> dict:
    """读取请求体 JSON：空 body 返回 {}，非法 JSON 抛 400（与 routes._safe_json 同口径）。"""
    raw = await req.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise HTTPException(400, "请求体不是合法 JSON")
    return data if isinstance(data, dict) else {}


# ========================================================================
# FAQ 知识库（assistant_faq.json，关键词命中直答，不走 LLM）
# ========================================================================

_FAQ_PATH = BASE_DIR / "assistant_faq.json"
_faqs: list[dict] | None = None


def _load_faqs() -> list[dict]:
    try:
        data = json.loads(_FAQ_PATH.read_text(encoding="utf-8"))
        return data.get("faqs", []) or []
    except Exception as e:
        logger.warning("FAQ 知识库加载失败: %r", e)
        return []


def get_faqs() -> list[dict]:
    global _faqs
    if _faqs is None:
        _faqs = _load_faqs()
    return _faqs


def match_faq(message: str) -> dict | None:
    """关键词匹配：统计每条 FAQ 命中关键词个数，取最高分（>0 才算命中）。
    同分时取「命中的最长关键词」更长的条目（如「记忆测试怎么用」应命中记忆测试
    而非宽泛的"怎么用"）。小写子串匹配，容忍大小写差异。"""
    msg = message.lower()
    best, best_key = None, (-1, -1)
    for faq in get_faqs():
        kws = faq.get("keywords", []) or []
        hits = [kw for kw in kws if kw and kw.lower() in msg]
        if not hits:
            continue
        key = (len(hits), max(len(kw) for kw in hits))
        if key > best_key:
            best, best_key = faq, key
    return best if best_key[0] > 0 else None


# 意图覆写：真实的查词/复习/加词/删词操作直接走 LLM 工具链，避免 FAQ 抢答
# （如「今天该复习哪些词」命中 FAQ 的"待复习"关键词，但应返回真实复习队列）
_INTENT_OVERRIDES: list[tuple[re.Pattern, str]] = [
    # 查一下 + 具体单词 → 查词
    (re.compile(r"(?i)(查一下|查查|查下|帮我查|能不能查)[\s\S]*\b[a-z][a-z\-']{1,40}\b"), "search_words"),
    # 具体单词 + 释义询问 → 查词
    (re.compile(r"(?i)\b[a-z][a-z\-']{1,40}\s*(是什么意思|啥意思|什么意思|意思)"), "search_words"),
    # 复习 + 哪些/多少/今天/到期 → 查复习队列
    (re.compile(r"(?i)(复习|到期)[\s\S]{0,10}(哪些|多少个|多少|今天|现在|该)"), "get_review_due"),
    (re.compile(r"(?i)(哪些|多少个|多少|今天|现在)[\s\S]{0,10}(复习|到期)"), "get_review_due"),
    # 加词操作（动词 + 词表）
    (re.compile(r"(?i)(加|添加|导入|收录|入库)[\s\S]*(,|，|、|\s|\d)[\s\S]*[a-z]"), "add_words"),
    # 加词操作（把 X 加进/加入/添加进词库：词在动词前）
    (re.compile(r"(?i)(?:\b[a-z][a-z\-']{1,40}\b[\s\S]{0,14})+(加进|加入|添加进|收进|录进)[\s\S]{0,4}(词库|库里|单词库)"), "add_words"),
    # 删词操作（动词 + 单词）
    (re.compile(r"(?i)(删|删除|移除|去掉|踢掉|不要|移出)[\s\S]*\b[a-z][a-z\-']{1,40}\b"), "delete_word"),
    # 删词操作（把 X 删掉 这类：词在动词前）
    (re.compile(r"(?i)\b[a-z][a-z\-']{1,40}\b[\s\S]{0,10}(删掉|删了|删除|移除|去掉)"), "delete_word"),
]


def _match_override(message: str) -> str | None:
    for pattern, tool in _INTENT_OVERRIDES:
        if pattern.search(message):
            return tool
    return None


# ========================================================================
# 工具定义（集中注册表：新增/修改工具只改这一处）
# ========================================================================
# confirm_required=True 为写操作：后端只产意图，由前端确认后调真实 API。

TOOL_SEARCH_WORDS = {
    "type": "function",
    "function": {
        "name": "search_words",
        "description": "在用户的单词库中查找单词（精确或模糊），返回词性/中文释义/音标/频率。用户问某词什么意思、查一下某词时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "要查找的单词或关键字（小写）"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
            },
            "required": ["keyword"],
        },
    },
}

TOOL_GET_REVIEW_DUE = {
    "type": "function",
    "function": {
        "name": "get_review_due",
        "description": "查询用户记忆测试中今日到期（含过期堆积）的复习单词列表。用户问'今天该复习哪些词、待复习多少'时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
            },
        },
    },
}

TOOL_ADD_WORDS = {
    "type": "function",
    "function": {
        "name": "add_words",
        "description": "【写操作·需用户确认】把一批英文单词批量加入用户的单词库（重复词自动跳过）。用户明确要求加词/导入一批词时调用。调用不等于执行，系统会先弹出确认卡片。",
        "parameters": {
            "type": "object",
            "properties": {
                "words": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要添加的英文单词列表（纯单词，转小写、去重、去标点）",
                },
            },
            "required": ["words"],
        },
    },
}

TOOL_DELETE_WORD = {
    "type": "function",
    "function": {
        "name": "delete_word",
        "description": "【写操作·需用户确认】从用户的单词库中删除一个单词（软删除，可撤销）。用户明确要求删除某词时调用。调用不等于执行，系统会先弹出确认卡片并展示该词中文释义防删错。",
        "parameters": {
            "type": "object",
            "properties": {
                "word": {"type": "string", "description": "要删除的单词（小写，单个词）"},
            },
            "required": ["word"],
        },
    },
}

TOOL_SCHEMAS = [TOOL_SEARCH_WORDS, TOOL_GET_REVIEW_DUE, TOOL_ADD_WORDS, TOOL_DELETE_WORD]


def _clean_words(raw) -> list[str]:
    """清洗 add_words 参数：仅保留合法英文字母单词，转小写、去重。"""
    words, seen = [], set()
    for w in raw or []:
        w = re.sub(r"[^a-zA-Z\-']", "", str(w).strip().lower())
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            words.append(w)
    return words


# ---------- 查询工具处理器（后端直接执行） ----------

async def _search_words(args: dict) -> dict:
    kw = str(args.get("keyword", "")).strip().lower()
    if not kw:
        return {"items": [], "matched": 0, "mode": "empty"}
    limit = min(int(args.get("limit") or 10), 30)
    conn = get_db()
    try:
        exact = conn.execute(
            "SELECT word, pos, meaning_zh, phonetic, frequency_level, healed_at FROM words WHERE word=?",
            (kw,),
        ).fetchone()
        if exact:
            return {"items": [dict(exact)], "matched": 1, "mode": "exact"}
        rows = conn.execute(
            "SELECT word, pos, meaning_zh, phonetic, frequency_level, healed_at FROM words "
            "WHERE word LIKE ? OR meaning_zh LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{kw}%", f"%{kw}%", limit),
        ).fetchall()
        return {"items": [dict(r) for r in rows], "matched": len(rows), "mode": "fuzzy"}
    finally:
        conn.close()


async def _get_review_due(args: dict) -> dict:
    limit = min(int(args.get("limit") or 20), 30)
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT s.word AS word, s.box, s.next_review_at,
                      COALESCE(w.meaning_zh, '') AS meaning_zh, COALESCE(w.pos, '') AS pos
               FROM review_schedule s LEFT JOIN words w ON w.word = s.word
               WHERE s.next_review_at <= ? AND (w.healed_at IS NULL OR w.healed_at = '')
               ORDER BY s.next_review_at ASC LIMIT ?""",
            (now, limit),
        ).fetchall()
        answered_today = conn.execute(
            "SELECT COUNT(*) c FROM review_log WHERE substr(answered_at,1,10)=?", (now[:10],)
        ).fetchone()["c"]
        items = [dict(r) for r in rows]
        return {
            "items": items,
            "matched": len(items),
            "answered_today": answered_today,
            "daily_limit": REVIEW_DAILY_LIMIT,
        }
    finally:
        conn.close()


# ---------- 写操作：仅产出意图（不执行，参数摘要供确认卡片展示） ----------

def _humanize_add_words(args: dict, user: str) -> dict:
    words = _clean_words(args.get("words"))
    existed = set()
    if words:
        conn = get_db(user)
        try:
            for w in words:
                if conn.execute("SELECT 1 FROM words WHERE word=?", (w,)).fetchone():
                    existed.add(w)
        finally:
            conn.close()
    added = [w for w in words if w not in existed]
    return {
        "words": words,
        "will_add": added,
        "will_skip": sorted(existed),
        "summary": f"将添加 {len(added)} 个单词" + (f"，已存在 {len(existed)} 个将被跳过" if existed else ""),
        "risk": "新增单词直接写入你的词库（重复自动跳过）",
    }


def _humanize_delete_word(args: dict, user: str) -> dict:
    word = re.sub(r"[^a-zA-Z\-']", "", str(args.get("word", "")).strip().lower())
    meaning, exists = "", False
    if word:
        conn = get_db(user)
        try:
            row = conn.execute("SELECT meaning_zh FROM words WHERE word=?", (word,)).fetchone()
            if row:
                exists, meaning = True, row["meaning_zh"] or ""
        finally:
            conn.close()
    return {
        "word": word,
        "exists": exists,
        "meaning_zh": meaning,
        "summary": f"从词库中删除「{word}」" + (f"（{meaning}）" if meaning else ""),
        "risk": "删除为软删除，确认后仍可在 10 秒内撤销" if exists else "词库中未找到该单词，删除后可能无变化",
    }


# ---------- 工具注册表 & 汇报文案渲染 ----------

_QUERY_TOOLS = {"search_words": _search_words, "get_review_due": _get_review_due}
_WRITE_TOOLS = {"add_words": _humanize_add_words, "delete_word": _humanize_delete_word}
_ALL_TOOLS = set(_QUERY_TOOLS) | set(_WRITE_TOOLS)


def _render_search(r: dict) -> str:
    if not r.get("items"):
        return "词库里没有找到相关单词。如果这个词还没上岛，可以先在「顽固词上岛」把它导入。"
    lines = []
    for it in r["items"][:10]:
        px = f"{it['word']}：{it.get('pos') or ''} {it.get('meaning_zh') or ''}".rstrip()
        if it.get("phonetic"):
            px += f"〔{it['phonetic']}〕"
        lines.append(f"- {px}")
    more = f"\n（共 {r['matched']} 个，此处仅显示前 {len(r['items'])} 个）" if r.get("matched", 0) > 10 else ""
    return f"在词库中找到 {r['matched']} 个相关单词：\n" + "\n".join(lines) + more


def _render_review_due(r: dict) -> str:
    if not r.get("items"):
        return "今天没有到期的复习单词，休息一下吧（也可以去「记忆测试」页看看整体进度）。"
    lines = [f"- {it['word']}（{it.get('meaning_zh') or '暂无释义'}）" for it in r["items"] if it.get("word")]
    tail = f"\n每日上限 {r.get('daily_limit', 20)} 个，今日已答 {r.get('answered_today', 0)} 个。"
    return f"今天有 {r['matched']} 个单词待复习（含积压）：\n" + "\n".join(lines) + tail


_TEXT_RENDERERS = {
    "search_words": _render_search,
    "get_review_due": _render_review_due,
}


# ========================================================================
# 会话持久化（assistant_conversations，按用户隔离）
# ========================================================================

MAX_CONTEXT_MESSAGES = 16  # 最近 8 轮 × 2（user + assistant）


def save_message(user: str, role: str, content: str):
    if not content or not content.strip():
        return
    conn = get_db(user)
    try:
        conn.execute(
            "INSERT INTO assistant_conversations (user_id, role, content) VALUES (?,?,?)",
            (user, role, content),
        )
        conn.commit()
    finally:
        conn.close()


def load_context_messages(user: str, limit: int = MAX_CONTEXT_MESSAGES) -> list[dict]:
    """取最近 limit 条会话消息，按时间正序拼成 LLM 上下文。"""
    conn = get_db(user)
    try:
        rows = conn.execute(
            "SELECT role, content FROM assistant_conversations WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user, limit),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in reversed(rows):
        if r["role"] in ("user", "assistant") and r["content"].strip():
            out.append({"role": r["role"], "content": r["content"]})
    return out


def fetch_history(user: str, limit: int = 50) -> list[dict]:
    conn = get_db(user)
    try:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM assistant_conversations "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user, max(min(limit, 200), 1)),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


def clear_history(user: str):
    conn = get_db(user)
    try:
        conn.execute("DELETE FROM assistant_conversations WHERE user_id=?", (user,))
        conn.commit()
    finally:
        conn.close()


# ========================================================================
# Agent 主流程：意图识别 → 工具调用 → 汇报
# ========================================================================

_SYSTEM_PROMPT = """你是「词小屿」，词屿（WordIsle）——一个英语顽固词疗养网站的贴心向导与受控操作员。
你已接入词屿官方 FAQ 知识库与 4 个工具，请做好这 3 件事：

1. 向导：介绍功能、回答"怎么用/在哪/是什么"。词屿共 14 个模块：
   单词库（查/增/改/删/发音/音标/治愈自评）、顽固词上岛（批量导入/文章提词）、
   单点深耕（单个词的记忆钩子卡片）、场景聚汇（场景词伙）、批量编译（连环画故事）、
   视频编译（短视频）、熟词僻意（一词多义/商务义）、构词拆解（词根树）、
   记忆测试（Leitner 间隔复习）、治愈图鉴、历史记录、用量情况、反馈看板、设置。
2. 操作员：用户明确要求查词、查复习状态、加词、删词时，必须调用对应工具，不要自己编造数据。
3. 诚实助手：回答简洁、口语化中文；不确定的事情明确说"这个问题我还不确定"，绝不编造功能或单词数据。

安全铁律（必须绝对遵守）：
- search_words / get_review_due 是查询工具：由系统执行后把结果交给你汇报，直接调用即可。
- add_words / delete_word 是写操作：你只能通过调用工具表达意图，系统会弹出确认卡片交给用户确认；
  调用≠执行，禁止在话术中谎称"已添加/已删除"，也不要在正文里自己假装改数据。
- add_words 的 words 参数必须是纯英文单词列表（转小写、去重）；delete_word 的 word 是单个单词。
- 用户没给出具体要删的单词时，先追问，不要猜词。"""

_PAGE_NAMES = {
    "home": "首页", "words": "单词库", "import": "顽固词上岛", "single": "单点深耕",
    "scenes": "场景聚汇", "compile": "批量编译", "video": "视频编译",
    "polysemy": "熟词僻意", "morphemes": "构词拆解", "review": "记忆测试",
    "healed": "治愈图鉴", "history": "历史记录", "usage": "用量情况", "dashboard": "反馈看板",
    "settings": "设置",
}


def _build_system_prompt(page: str) -> str:
    name = _PAGE_NAMES.get(page, "首页")
    return f"{_SYSTEM_PROMPT}\n用户当前所在页面是「{name}」，回答时优先结合当前页。" if name else _SYSTEM_PROMPT


def _assistant_for_scope() -> str:
    """返回当前请求的用户 uid（认证依赖已写入 contextvar）。"""
    return current_uid.get(None) or DEV_USERNAME


async def chat_stream(user: str, message: str, page: str):
    """词小屿单轮对话主生成器（SSE 事件流）。"""
    text = message.strip()
    if not text:
        yield ("error", {"msg": "消息不能为空"})
        return

    save_message(user, "user", text)

    # -------- L0：FAQ 命中直答（意图覆写优先，避免抢答真实操作） --------
    if not _match_override(text):
        faq = match_faq(text)
        if faq:
            consume_daily_quota("ai")
            answer = faq.get("answer", "") or "……"
            related = faq.get("related_page", "")
            if related:
                yield ("tool", {"tool": "navigate", "args": {"page": related}, "human_readable": str(faq.get("question", "")), "confirm_required": False})
            async for evt, data in _stream_text(answer):
                yield (evt, data)
            save_message(user, "assistant", answer)
            yield ("done", {"ok": True})
            return

    # -------- L1：LLM 意图识别（Function Calling，真实流式：首字秒出） --------
    # 意图覆写命中 → 在用户消息里显式指定工具（DashScope 思考模式不支持 tool_choice=对象，
    # 用指令让模型必然选该工具，模型无关且稳定）
    forced_tool = _match_override(text)
    yield ("step", {"step": "intent", "label": "识别意图", "status": "running"})
    context = load_context_messages(user)
    if forced_tool in _ALL_TOOLS:
        text = f"{text}\n（系统指令：请调用工具 {forced_tool} 完成本次请求，不要调用其他工具，也不要拒绝）"
    messages = [{"role": "system", "content": _build_system_prompt(page)}] + context + [
        {"role": "user", "content": text},
    ]

    # 真实流式：delta 逐字推到前端，流结束再汇总 content / tool_calls（真流式失败自动降级）
    full_parts: list[str] = []
    tool_calls: list = []
    final_content = ""
    model = ""
    got_any = False
    async for evt in stream_llm_with_fallback(
        messages, "assistant", temperature=0.3, max_tokens=1024, timeout=60.0,
        detail="智能助手", tools=TOOL_SCHEMAS, tool_choice="auto",
    ):
        if evt["type"] == "delta":
            got_any = True
            full_parts.append(evt["text"])
            yield ("result", {"text": evt["text"]})
        elif evt["type"] == "result":
            if evt["content"]:
                got_any = True
                final_content = evt["content"]
                if not full_parts:  # 非流式兜底：一次性给出全文
                    yield ("result", {"text": evt["content"]})
            if evt["tool_calls"]:
                tool_calls = evt["tool_calls"]
    consume_daily_quota("ai")

    if not got_any and not tool_calls:
        yield ("error", {"msg": "智能助手当前不可用（LLM 未配置或调用失败），请稍后再试，或在「设置」中检查模型配置"})
        return

    content = (final_content or "".join(full_parts)).strip()

    tool_calls = [tc for tc in tool_calls if isinstance(tc, dict) and tc.get("function")]
    if not tool_calls:
        save_message(user, "assistant", content)
        yield ("done", {"ok": True})
        return

    # -------- 工具调用（本版取第一条，保证可预期） --------
    fn = (tool_calls[0].get("function") or {}) if isinstance(tool_calls[0], dict) else {}
    name = str(fn.get("name") or "").strip()
    try:
        args = json.loads(fn.get("arguments") or "{}") if isinstance(fn, dict) else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    if name not in _QUERY_TOOLS and name not in _WRITE_TOOLS:
        fallback = content or "抱歉，我刚才没理解你的意图，请换一种说法再问一次。"
        async for evt, d in _stream_text(fallback):
            yield (evt, d)
        save_message(user, "assistant", fallback)
        yield ("done", {"ok": True})
        return

    # -------- 写操作：只产意图，前端渲染确认卡片（安全铁律） --------
    if name in _WRITE_TOOLS:
        human = _WRITE_TOOLS[name](args, user)
        # 参数缺失保护：LLM 没解析出具体词/单词 → 追问，不弹空卡片
        empty = (name == "add_words" and not human.get("words")) or \
                (name == "delete_word" and not human.get("word"))
        if empty:
            followup = ("还没收到具体的单词清单呢，把要加的词发我（如：帮我加 abandon、appreciate）" if name == "add_words"
                        else "你想删哪个单词呢？把单词发我，我再给你确认卡片（如：删掉 abandon）")
            async for evt, d in _stream_text(followup):
                yield (evt, d)
            save_message(user, "assistant", followup)
            yield ("done", {"ok": True})
            return
        yield ("step", {"step": "tool", "label": "准备操作", "model": model, "status": "ok"})
        yield ("tool", {"tool": name, "args": args, "human_readable": human, "confirm_required": True})
        intro = content or f"收到！{human.get('summary', '')}，请确认下面的操作卡片。"
        async for evt, d in _stream_text(intro):
            yield (evt, d)
        save_message(user, "assistant", f"{intro}（已弹出操作确认卡片，等待用户确认后执行）")
        yield ("done", {"ok": True})
        return

    # -------- 查询工具：后端直接执行并返回结构化数据 --------
    yield ("step", {"step": "tool", "label": "查询中", "model": model, "status": "running"})
    try:
        result = await _QUERY_TOOLS[name](args)
    except Exception as e:
        logger.warning("词小屿查询工具失败 [%s] args=%r error=%r", name, args, e)
        result = {"error": f"{name} 查询失败，请稍后再试"}

    human_desc = {
        "search_words": f"在词库中查找相关单词",
        "get_review_due": "查询今日到期的复习单词",
    }.get(name, f"{name} 查询")
    yield ("tool", {"tool": name, "args": args, "human_readable": human_desc, "confirm_required": False, "data": result})
    summary = _TEXT_RENDERERS.get(name, lambda r: "查询完成")(result)
    async for evt, d in _stream_text(summary):
        yield (evt, d)
    save_message(user, "assistant", summary)
    yield ("done", {"ok": True})


# ========================================================================
# HTTP 接口（SSE 流式，需登录）
# ========================================================================

@router.post("/api/assistant/chat")
async def assistant_chat(req: Request):
    """词小屿对话（SSE）：{message, page} → step/tool/result/done/error 事件流。"""
    body = await _safe_json(req)
    message = str(body.get("message", "")).strip()
    page = str(body.get("page", "home")).strip() or "home"
    if not message:
        raise HTTPException(400, "消息不能为空")

    # 每日对话配额拦截（游客 50 条/日，dev/admin 不限）。FAQ 命中同样计一次对话。
    require_quota("assistant")
    user = _assistant_for_scope()
    logger.info("词小屿对话开始 user=%s page=%s msg=%s", user, page, message[:60])
    return StreamingResponse(
        _sse_stream(chat_stream(user, message, page)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/assistant/history")
async def assistant_history(limit: int = 50):
    """读取当前用户最近会话（刷新不丢，恢复历史用）。"""
    user = _assistant_for_scope()
    items = fetch_history(user, limit)
    return {"items": items, "total": len(items)}


@router.delete("/api/assistant/conversation")
async def assistant_clear():
    """清空当前用户的会话记录。"""
    user = _assistant_for_scope()
    clear_history(user)
    logger.info("词小屿会话已清空 user=%s", user)
    return {"ok": True}


@router.get("/api/assistant/status")
async def assistant_status():
    """挂件状态：LLM 是否已配置、当前模型、FAQ 条数（挂件置灰判断用）。"""
    cfg = get_route_llm("assistant")
    return {
        "configured": bool(cfg.get("api_key")),
        "model": cfg.get("model", ""),
        "model_label": cfg.get("label", ""),
        "faq_count": len(get_faqs()),
    }