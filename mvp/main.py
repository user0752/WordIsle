"""
TOEIC 顽固词深度加工系统 - MVP 个人版
=========================================
单文件，零依赖基础设施。FastAPI + SQLite + DeepSeek + 百炼 TTS。

启动: pip install -r requirements.txt && cp .env.example .env && python main.py
访问: http://localhost:8000
"""

import asyncio
import json
import os
import re
import time
import hashlib
import uuid
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Any

import dashscope
import httpx
from dashscope.audio.tts_v2 import SpeechSynthesizer
from dashscope.audio.http_tts import HttpSpeechSynthesizer
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ========================================================================
# 配置
# ========================================================================

load_dotenv()

BASE_DIR    = Path(__file__).resolve().parent
DATA_DIR    = BASE_DIR / "data"
DB_PATH     = DATA_DIR / "words.db"
AUDIOS_DIR  = DATA_DIR / "audios"
AUDIOS_DIR.mkdir(parents=True, exist_ok=True)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE    = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL   = os.getenv("DEEPSEEK_MODEL_NAME", "deepseek-chat")

TTS_API_KEY   = os.getenv("TTS_API_KEY", "")
TTS_ENDPOINT  = os.getenv("TTS_ENDPOINT", "https://dashscope.aliyuncs.com/api/v1/services/audio/tts")
TTS_VOICE     = os.getenv("TTS_VOICE", "loongandy_v3")
TTS_MODEL     = os.getenv("TTS_MODEL", "cosyvoice-v3-flash")

DAILY_AI_LIMIT  = int(os.getenv("DAILY_AI_LIMIT", "20"))
DAILY_TTS_LIMIT = int(os.getenv("DAILY_TTS_LIMIT", "50"))

# ========================================================================
# SQLite 数据库
# ========================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE,
            lemma TEXT DEFAULT '',
            source TEXT DEFAULT '',
            difficulty TEXT DEFAULT 'intermediate',
            stubborn_score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            note TEXT DEFAULT '',
            original_input TEXT DEFAULT '',
            has_polysemy INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY,
            words TEXT NOT NULL,
            content_form TEXT DEFAULT 'dialogue',
            scene_type   TEXT DEFAULT 'meeting',
            difficulty TEXT DEFAULT 'intermediate',
            length_level TEXT DEFAULT 'medium',
            title        TEXT DEFAULT '',
            body_en      TEXT NOT NULL,
            body_zh      TEXT DEFAULT '',
            model        TEXT DEFAULT '',
            word_forms   TEXT DEFAULT '{}',
            collocations TEXT DEFAULT '[]',
            polysemy_notes  TEXT DEFAULT '{}',
            included_words  TEXT DEFAULT '[]',
            missing_words   TEXT DEFAULT '[]',
            toc_part_tags TEXT DEFAULT '[]',
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
            tts_count INTEGER DEFAULT 0
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
        pass  # 列已存在则忽略
    conn.commit()
    conn.close()

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

def consume_daily_quota(category: str) -> bool:
    """原子地检查并占用一次当日配额（BEGIN IMMEDIATE 事务，超限返回 False）。"""
    today = date.today().isoformat()
    col = "ai_count" if category == "ai" else "tts_count"
    limit = DAILY_AI_LIMIT if category == "ai" else DAILY_TTS_LIMIT
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM daily_usage WHERE day=?", (today,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO daily_usage(day) VALUES(?)", (today,))
            count = 0
        else:
            count = row[col]
        if count >= limit:
            conn.execute("ROLLBACK")
            return False
        conn.execute(f"UPDATE daily_usage SET {col}={col}+1 WHERE day=?", (today,))
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

# ========================================================================
# DeepSeek / 百炼 TTS 调用
# ========================================================================

# ---- Prompt（复用原始项目的系统 Prompt，精简保留核心约束） ----

SYSTEM_PROMPT = """You are a TOEIC Business English content writer. Your audience is TOEIC test-takers who need to master stubborn vocabulary through real workplace scenarios.

RULES:
1. Content must be natural, professional, and business-realistic (meetings, emails, travel, procurement, HR, finance, customer service, project management, marketing).
2. Include ALL target words naturally; use common inflections if needed (reimburse->reimbursement). If a word truly cannot fit, list it in missing_words—never force it.
3. Highlight "polysemy" words (words with special business meanings) in polysemy_notes.
4. Difficulty and length must match the user's request.
5. Output ONLY a valid JSON object. No markdown, no extra text.

JSON STRUCTURE:
{
  "title": "English title",
  "content_form": "dialogue|email|memo|report",
  "scene": "brief Chinese scene description",
  "body_en": "full English text",
  "body_zh": "Chinese translation",
  "target_words": ["word1","word2"],
  "included_words": ["word1","word2"],
  "missing_words": [],
  "word_forms": {"original":"form used in text"},
  "collocations": ["business collocation 1","business collocation 2"],
  "polysemy_notes": {"word":"explanation of its business meaning here"},
  "difficulty": "intermediate",
  "toc_part_tags": ["part3","part7"],
  "naturalness_score": 8,
  "warnings": []
}
"""

def build_user_prompt(words: list[str], content_form="dialogue", scene_type="meeting", include_translation=True):
    content_desc = {
        "dialogue": "Business dialogue (2-4 people, e.g. meeting discussion, phone call, client chat)",
        "email": "Business email (formal professional email)",
        "memo": "Internal memo (concise official notice)",
        "report": "Business report (report summary or brief)",
        "short_narrative": "Short narrative passage (a brief story describing a workplace scenario)",
    }.get(content_form, content_form)

    scene_desc = {
        "meeting": "Company meeting",
        "email": "Email correspondence",
        "travel": "Business travel",
        "procurement": "Procurement/Supplier negotiation",
        "hr": "Human Resources",
        "finance": "Finance/Accounting",
        "customer_service": "Customer Service",
        "project_management": "Project Management",
        "marketing": "Marketing/Promotion",
    }.get(scene_type, scene_type)

    words_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))

    return f"""Please write TOEIC business English study material:

TARGET WORDS ({len(words)} total):
{words_list}

CONTENT FORM: {content_desc}

SCENE TYPE: {scene_desc}

DIFFICULTY: Intermediate (TOEIC level)
LENGTH: ~200-350 words
INCLUDE CHINESE TRANSLATION: {'Yes' if include_translation else 'No'}

Make sure all target words appear naturally in the business context. Output only the JSON object."""

async def call_deepseek(words: list[str], content_form="dialogue", scene_type="meeting", include_translation=True):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "请先设置 DEEPSEEK_API_KEY 环境变量")

    user_prompt = build_user_prompt(words, content_form, scene_type, include_translation)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"].strip()
    # 去掉可能的 markdown 代码块
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(content), data.get("usage", {})

async def call_tts(text: str, voice=None, speed=1.0, model=None):
    if not TTS_API_KEY:
        raise HTTPException(500, "请先设置 TTS_API_KEY 环境变量")

    dashscope.api_key = TTS_API_KEY
    voice_name = voice or TTS_VOICE
    model_name = model or TTS_MODEL

    # 使用 HTTP API（非 WebSocket），避免网络环境对 WebSocket 的限制
    loop = asyncio.get_running_loop()
    try:
        # 第一步：调用 HTTP TTS API，获取音频下载 URL
        result = await loop.run_in_executor(
            None,
            lambda: HttpSpeechSynthesizer.call(
                model=model_name,
                text=text,
                voice=voice_name,
                audio_format="mp3",
                stream=False,
                rate=speed,
            ),
        )
    except Exception as e:
        raise HTTPException(500, f"TTS 合成请求失败 ({model_name}/{voice_name}): {e}")

    if not result or not result.audio_url:
        msg = (result.message or "返回空结果") if result else "返回空结果"
        raise HTTPException(500, f"TTS 合成失败: {msg}")

    # 第二步：下载音频文件
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            audio_resp = await client.get(result.audio_url)
            audio_resp.raise_for_status()
            audio_bytes = audio_resp.content
    except Exception as e:
        raise HTTPException(500, f"TTS 音频下载失败: {e}")

    if audio_bytes:
        return audio_bytes
    else:
        raise HTTPException(500, "TTS 合成失败: 返回空音频")

# ========================================================================
# FastAPI 应用
# ========================================================================

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="TOEIC MVP", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 静态文件：音频目录
app.mount("/audios", StaticFiles(directory=str(AUDIOS_DIR)), name="audios")

# ========================================================================
# API 路由
# ========================================================================

@app.post("/api/generate")
async def generate(req: Request):
    body = await req.json()
    raw_words = body.get("words", "")
    content_form = body.get("content_form", "dialogue")
    scene_type   = body.get("scene_type", "meeting")
    difficulty   = body.get("difficulty", "intermediate")
    length_level = body.get("length_level", "medium")
    include_translation = body.get("include_translation", True)
    generate_audio = body.get("generate_audio_immediately", False)
    tts_model = body.get("tts_model", TTS_MODEL) if generate_audio else None

    words = normalize_words(raw_words)
    if not words:
        raise HTTPException(400, "请至少输入一个有效单词")
    if len(words) > 30:
        raise HTTPException(400, f"单次最多 30 个单词，当前 {len(words)} 个")

    if not consume_daily_quota("ai"):
        raise HTTPException(429, f"今日 AI 生成已达上限 ({DAILY_AI_LIMIT} 次)")

    gen_id   = str(uuid.uuid4())[:8]
    result, usage = await call_deepseek(words, content_form, scene_type, include_translation)

    length_map = {"short": "~80 words", "medium": "~150 words", "long": "~250 words"}

    # 入库
    conn = get_db()
    conn.execute("""
        INSERT INTO generations (id,words,content_form,scene_type,difficulty,length_level,
                                 title,body_en,body_zh,model,
                                 word_forms,collocations,polysemy_notes,included_words,missing_words,toc_part_tags)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        gen_id,
        json.dumps(words),
        content_form,
        scene_type,
        difficulty,
        length_level,
        result.get("title",""),
        result.get("body_en",""),
        result.get("body_zh","") if include_translation else "",
        DEEPSEEK_MODEL,
        json.dumps(result.get("word_forms",{})),
        json.dumps(result.get("collocations",[])),
        json.dumps(result.get("polysemy_notes",{})),
        json.dumps(result.get("included_words",[])),
        json.dumps(result.get("missing_words",[])),
        json.dumps(result.get("toc_part_tags",[])),
    ))
    for w in words:
        conn.execute("INSERT OR IGNORE INTO words(word,original_input) VALUES(?,?)", (w, w))
    conn.commit()
    conn.close()

    resp = {
        "id": gen_id,
        "status": "success",
        "title": result.get("title"),
        "body_en": result.get("body_en"),
        "body_zh": result.get("body_zh") if include_translation else "",
        "words": words,
        "included_words": result.get("included_words", []),
        "missing_words": result.get("missing_words", []),
        "collocations": result.get("collocations", []),
        "polysemy_notes": result.get("polysemy_notes", {}),
        "word_forms": result.get("word_forms", {}),
        "toc_part_tags": result.get("toc_part_tags", []),
        "content_form": content_form,
        "scene_type": scene_type,
        "difficulty": difficulty,
        "scene": result.get("scene", ""),
        "has_audio": False,
        "audio_id": None,
    }

    # 如果需要立即生成音频
    if generate_audio and result.get("body_en"):
        if not consume_daily_quota("tts"):
            resp["audio_error"] = f"今日 TTS 合成已达上限 ({DAILY_TTS_LIMIT} 次)，未生成音频"
        else:
            try:
                audio_bytes = await call_tts(result["body_en"], TTS_VOICE, 1.0, tts_model)
                file_name = f"{gen_id}_{TTS_VOICE}_100.mp3"
                file_path = AUDIOS_DIR / file_name
                file_path.write_bytes(audio_bytes)
                conn = get_db()
                cur = conn.execute(
                    "INSERT INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
                    (gen_id, file_name, TTS_VOICE, 1.0, tts_model),
                )
                conn.commit()
                conn.close()
                resp["has_audio"] = True
                resp["audio_id"] = cur.lastrowid
                resp["audio_url"] = f"/audios/{file_name}"
                resp["tts_model"] = tts_model
            except HTTPException as e:
                resp["audio_error"] = e.detail
            except Exception as e:
                resp["audio_error"] = f"音频生成失败: {e}"

    return resp


@app.post("/api/generations/{gen_id}/audio")
async def generate_audio(gen_id: str, req: Request):
    body = await req.json() if await req.body() else {}
    voice = body.get("voice", TTS_VOICE)
    speed = body.get("speed", 1.0)
    tts_model = body.get("tts_model", TTS_MODEL)
    voice, speed = validate_tts_params(voice, speed)

    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if not gen:
        conn.close()
        raise HTTPException(404, "生成记录不存在")

    # 先去重：同一文本同一音色同一模型的音频已存在则直接返回
    existing = conn.execute(
        "SELECT * FROM audios WHERE generation_id=? AND voice=? AND speed=? AND tts_model=?",
        (gen_id, voice, speed, tts_model),
    ).fetchone()
    if existing:
        conn.close()
        return {
            "id": existing["id"], "file_name": existing["file_name"],
            "url": f"/audios/{existing['file_name']}", "cached": True,
            "tts_model": tts_model,
        }

    if not consume_daily_quota("tts"):
        conn.close()
        raise HTTPException(429, f"今日 TTS 合成已达上限 ({DAILY_TTS_LIMIT} 次)")

    conn.close()

    audio_bytes = await call_tts(gen["body_en"], voice, speed, tts_model)
    file_name = f"{gen_id}_{voice}_{int(speed*100)}.mp3"
    file_path = AUDIOS_DIR / file_name
    file_path.write_bytes(audio_bytes)

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
        (gen_id, file_name, voice, speed, tts_model),
    )
    audio_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {"id": audio_id, "generation_id": gen_id, "file_name": file_name, "url": f"/audios/{file_name}", "cached": False, "tts_model": tts_model}


@app.get("/api/generations")
async def list_generations():
    conn = get_db()
    rows = conn.execute("""
        SELECT g.*, a.file_name as audio_file
        FROM generations g
        LEFT JOIN (SELECT generation_id, MIN(file_name) as file_name FROM audios GROUP BY generation_id) a
          ON g.id = a.generation_id
        ORDER BY g.created_at DESC LIMIT 50
    """).fetchall()
    conn.close()
    return [{
        "id": r["id"],
        "title": r["title"],
        "words": json.loads(r["words"] or "[]"),
        "content_form": r["content_form"],
        "scene_type": r["scene_type"],
        "difficulty": r["difficulty"],
        "created_at": r["created_at"],
        "body_en": (r["body_en"][:100] + "...") if r["body_en"] and len(r["body_en"]) > 100 else (r["body_en"] or ""),
        "has_audio": bool(r["audio_file"]),
        "is_favorited": bool(r["is_favorited"]),
        "included_words": json.loads(r["included_words"] or "[]"),
        "missing_words": json.loads(r["missing_words"] or "[]"),
    } for r in rows]


@app.get("/api/generations/{gen_id}")
async def get_generation(gen_id: str):
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    aud = conn.execute("SELECT * FROM audios WHERE generation_id=? LIMIT 1", (gen_id,)).fetchone()
    conn.close()
    if not gen:
        raise HTTPException(404, "记录不存在")
    return {
        "id": gen["id"],
        "title": gen["title"],
        "body_en": gen["body_en"],
        "body_zh": gen["body_zh"],
        "words": json.loads(gen["words"] or "[]"),
        "content_form": gen["content_form"],
        "scene_type": gen["scene_type"],
        "difficulty": gen["difficulty"],
        "length_level": gen["length_level"],
        "included_words": json.loads(gen["included_words"] or "[]"),
        "missing_words": json.loads(gen["missing_words"] or "[]"),
        "collocations": json.loads(gen["collocations"] or "[]"),
        "polysemy_notes": json.loads(gen["polysemy_notes"] or "{}"),
        "word_forms": json.loads(gen["word_forms"] or "{}"),
        "toc_part_tags": json.loads(gen["toc_part_tags"] or "[]"),
        "is_favorited": bool(gen["is_favorited"]),
        "created_at": gen["created_at"],
        "audio_url": f"/audios/{aud['file_name']}" if aud else None,
        "audio_id": aud["id"] if aud else None,
        "has_audio": bool(aud),
        "tts_model": aud["tts_model"] if aud else None,
    }


@app.delete("/api/generations/{gen_id}")
async def delete_generation(gen_id: str):
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if not gen:
        conn.close()
        raise HTTPException(404, "记录不存在")
    auds = conn.execute("SELECT file_name FROM audios WHERE generation_id=?", (gen_id,)).fetchall()
    for a in auds:
        (AUDIOS_DIR / a["file_name"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM generations WHERE id=?", (gen_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/health")
async def health():
    today = date.today().isoformat()
    conn = get_db()
    row = conn.execute("SELECT * FROM daily_usage WHERE day=?", (today,)).fetchone()
    conn.close()
    usage = {"ai": 0, "tts": 0}
    if row:
        usage = {"ai": row["ai_count"], "tts": row["tts_count"]}
    return {
        "status": "ok",
        "db": DB_PATH.exists(),
        "deepseek_key": bool(DEEPSEEK_API_KEY),
        "tts_key": bool(TTS_API_KEY),
        "daily_usage": {**usage, "ai_limit": DAILY_AI_LIMIT, "tts_limit": DAILY_TTS_LIMIT},
    }

# ========================================================================
# 音频 API
# ========================================================================

@app.get("/api/audios/{audio_id}")
async def get_audio(audio_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM audios WHERE id=?", (audio_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "音频不存在")
    return dict(row)

@app.get("/api/audios/{audio_id}/stream")
async def stream_audio(audio_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM audios WHERE id=?", (audio_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "音频不存在")
    file_path = AUDIOS_DIR / row["file_name"]
    if not file_path.exists():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(str(file_path), media_type="audio/mpeg")

# ========================================================================
# 单词管理 API
# ========================================================================

@app.get("/api/words")
async def list_words(page: int = 1, page_size: int = 20, search: str = ""):
    conn = get_db()
    offset = (page - 1) * page_size
    if search:
        rows = conn.execute(
            "SELECT * FROM words WHERE word LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (f"%{search}%", page_size, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM words WHERE word LIKE ?", (f"%{search}%",)).fetchone()[0]
    else:
        rows = conn.execute(
            "SELECT * FROM words ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    conn.close()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}

@app.post("/api/words")
async def create_word(req: Request):
    body = await req.json()
    word = body.get("word", "").strip().lower()
    if not word or len(word) < 2:
        raise HTTPException(400, "无效单词")
    source = body.get("source", "")
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO words (word, source, original_input) VALUES (?,?,?)", (word, source, word))
        wid = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"单词 '{word}' 已存在")
    finally:
        conn.close()

@app.patch("/api/words/{word_id}")
async def update_word(word_id: int, req: Request):
    body = await req.json()
    allowed = {"status", "note", "difficulty", "stubborn_score", "source"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "无有效字段")
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [word_id]
    conn = get_db()
    conn.execute(f"UPDATE words SET {sets} WHERE id=?", vals)
    conn.commit()
    row = conn.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "单词不存在")
    return dict(row)

@app.delete("/api/words/{word_id}")
async def delete_word(word_id: int):
    conn = get_db()
    conn.execute("DELETE FROM words WHERE id=?", (word_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/words/parse")
async def parse_words(req: Request):
    body = await req.json()
    text = body.get("text", "")
    words = normalize_words(text)
    conn = get_db()
    existing = set(r["word"] for r in conn.execute("SELECT word FROM words").fetchall())
    # 查找熟词生意（在关闭连接前完成所有查询）
    polysemy_words = []
    for w in words:
        r = conn.execute("SELECT word FROM polysemy WHERE word=?", (w,)).fetchone()
        if r:
            polysemy_words.append(w)
    conn.close()
    duplicate_count = sum(1 for w in words if w in existing)
    invalid_count = 0
    return {
        "words": words,
        "duplicate_count": duplicate_count,
        "invalid_count": invalid_count,
        "polysemy_matched": len(polysemy_words),
    }

@app.post("/api/words/import")
async def import_words(req: Request):
    body = await req.json()
    word_list = body.get("words", [])
    source = body.get("source", "manual_import")
    conn = get_db()
    imported = 0
    duplicated = 0
    for w in word_list:
        w = w.strip().lower()
        if not w or len(w) < 2:
            continue
        try:
            conn.execute("INSERT INTO words (word, source, original_input) VALUES (?,?,?)", (w, source, w))
            imported += 1
        except sqlite3.IntegrityError:
            duplicated += 1
    conn.commit()
    conn.close()
    return {"imported": imported, "duplicated": duplicated, "total_input": len(word_list)}

# ========================================================================
# 生成文本管理 API
# ========================================================================

@app.get("/api/texts")
async def list_texts(page: int = 1, content_form: str = ""):
    conn = get_db()
    offset = (page - 1) * 20
    where = ""
    params: list[Any] = []
    if content_form:
        where = "WHERE content_form=?"
        params.append(content_form)
    rows = conn.execute(
        f"SELECT * FROM generations {where} ORDER BY created_at DESC LIMIT 20 OFFSET ?",
        params + [offset],
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM generations {where}", params).fetchone()[0]
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d["words"] = json.loads(d.get("words", "[]"))
        d["included_words"] = json.loads(d.get("included_words", "[]"))
        d["missing_words"] = json.loads(d.get("missing_words", "[]"))
        d["toc_part_tags"] = json.loads(d.get("toc_part_tags", "[]"))
        d["collocations"] = json.loads(d.get("collocations", "[]"))
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": 20}

@app.get("/api/texts/{text_id}")
async def get_text(text_id: str):
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (text_id,)).fetchone()
    conn.close()
    if not gen:
        raise HTTPException(404, "文本不存在")
    d = dict(gen)
    d["words"] = json.loads(d.get("words", "[]"))
    d["included_words"] = json.loads(d.get("included_words", "[]"))
    d["missing_words"] = json.loads(d.get("missing_words", "[]"))
    d["collocations"] = json.loads(d.get("collocations", "[]"))
    d["polysemy_notes"] = json.loads(d.get("polysemy_notes", "{}"))
    d["word_forms"] = json.loads(d.get("word_forms", "{}"))
    d["toc_part_tags"] = json.loads(d.get("toc_part_tags", "[]"))
    return d

@app.post("/api/texts/{text_id}/favorite")
async def favorite_text(text_id: str, req: Request):
    body = await req.json()
    favorited = body.get("favorited", False)
    conn = get_db()
    conn.execute("UPDATE generations SET is_favorited=? WHERE id=?", (1 if favorited else 0, text_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/texts/{text_id}")
async def delete_text(text_id: str):
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (text_id,)).fetchone()
    if not gen:
        raise HTTPException(404, "文本不存在")
    auds = conn.execute("SELECT file_name FROM audios WHERE generation_id=?", (text_id,)).fetchall()
    for a in auds:
        (AUDIOS_DIR / a["file_name"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM generations WHERE id=?", (text_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/texts/{text_id}/regenerate-audio")
async def regenerate_audio_for_text(text_id: str, req: Request):
    body = await req.json() if await req.body() else {}
    voice = body.get("voice", TTS_VOICE)
    speed = body.get("speed", 1.0)
    tts_model = body.get("tts_model", TTS_MODEL)
    voice, speed = validate_tts_params(voice, speed)
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (text_id,)).fetchone()
    conn.close()
    if not gen:
        raise HTTPException(404, "文本不存在")
    if not gen["body_en"]:
        raise HTTPException(400, "文本无英文内容")
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM audios WHERE generation_id=? AND voice=? AND speed=? AND tts_model=?",
        (text_id, voice, speed, tts_model),
    ).fetchone()
    if existing:
        conn.close()
        return {
            "id": existing["id"], "generation_id": text_id,
            "file_name": existing["file_name"], "url": f"/audios/{existing['file_name']}",
            "cached": True, "tts_model": tts_model,
        }
    conn.close()
    if not consume_daily_quota("tts"):
        raise HTTPException(429, f"今日 TTS 合成已达上限 ({DAILY_TTS_LIMIT} 次)")
    audio_bytes = await call_tts(gen["body_en"], voice, speed, tts_model)
    file_name = f"{text_id}_{voice}_{int(speed*100)}.mp3"
    file_path = AUDIOS_DIR / file_name
    file_path.write_bytes(audio_bytes)
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
        (text_id, file_name, voice, speed, tts_model),
    )
    audio_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": audio_id, "generation_id": text_id, "file_name": file_name, "url": f"/audios/{file_name}", "cached": False, "tts_model": tts_model}

# ========================================================================
# 熟词生意 API
# ========================================================================

@app.get("/api/polysemy")
async def get_polysemy(word: str = ""):
    if not word:
        raise HTTPException(400, "请提供单词")
    conn = get_db()
    row = conn.execute("SELECT * FROM polysemy WHERE word=?", (word.strip().lower(),)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["collocations"] = json.loads(d.get("collocations", "[]"))
    return d

@app.get("/api/polysemy/hot")
async def polysemy_hot(page: int = 1):
    conn = get_db()
    offset = (page - 1) * 20
    rows = conn.execute("SELECT * FROM polysemy ORDER BY frequency_level DESC LIMIT 20 OFFSET ?", (offset,)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM polysemy").fetchone()[0]
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d["collocations"] = json.loads(d.get("collocations", "[]"))
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": 20}

@app.post("/api/polysemy/mark")
async def mark_polysemy(req: Request):
    body = await req.json()
    word = body.get("word", "")
    marked = body.get("marked_as_difficult", False)
    if not word:
        raise HTTPException(400, "请提供单词")
    conn = get_db()
    conn.execute("UPDATE words SET has_polysemy=? WHERE word=?", (1 if marked else 0, word.strip().lower()))
    conn.commit()
    conn.close()
    return {"ok": True}

# ========================================================================
# 前端（嵌入式单页 HTML）
# ========================================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TOEIC 顽固词深度加工系统</title>
<link rel="stylesheet" href="https://unpkg.com/element-plus/dist/index.css">
<style>
  /* ================================================
     设计系统：单词卡片桌 — Card Desk Study
     最适合记单词的场景设计
     就像在暖色台灯下翻看纸质单词卡片
     ================================================ */
  :root {
    /* 桌面底色 — 暖灰布纹，像书桌表面 */
    --bg-warm: #f7f3ed;
    --bg-paper: #faf7f3;
    --bg-card: #fefcf9;
    --bg-card-hover: #fffdfa;
    --bg-surface: #f3efe8;
    --bg-elevated: #f8f5f0;

    /* 墨色文字 — 温暖深灰，适合长时间阅读 */
    --text-primary: #2c2a26;
    --text-secondary: #6b6560;
    --text-tertiary: #9c948c;
    --text-muted: #b8b0a8;

    /* 墨绿 — 主色：沉稳安静，像书房里的绿植，适合长时间专注 */
    --ink: #2d6a5e;
    --ink-light: #3d8a7a;
    --ink-dark: #1d5a4e;
    --ink-bg: rgba(45, 106, 94, 0.06);
    --ink-border: rgba(45, 106, 94, 0.15);
    --ink-glow: rgba(45, 106, 94, 0.15);

    /* 金琥珀 — 点缀色：像蜜糖，温暖而克制 */
    --gold: #c49a3a;
    --gold-light: #d8ae4e;
    --gold-bg: rgba(196, 154, 58, 0.08);
    --gold-border: rgba(196, 154, 58, 0.2);
    --gold-glow: rgba(196, 154, 58, 0.15);

    /* 荧光黄 — 高亮标记，像荧光笔划过 */
    --marker-yellow: #f0d060;
    --marker-yellow-bg: rgba(240, 208, 96, 0.15);

    /* 边框 — 柔和隐约 */
    --border-subtle: rgba(44, 42, 38, 0.06);
    --border-default: rgba(44, 42, 38, 0.1);
    --border-strong: rgba(44, 42, 38, 0.16);
    --border-ink: rgba(45, 106, 94, 0.2);

    /* 语义色 — 更柔和的版本 */
    --success-color: #2d7a5a;
    --success-bg: rgba(45, 122, 90, 0.08);
    --success-border: rgba(45, 122, 90, 0.2);
    --warning-color: #b8860b;
    --warning-bg: rgba(184, 134, 11, 0.08);
    --warning-border: rgba(184, 134, 11, 0.2);
    --danger-color: #b94a3a;
    --danger-bg: rgba(185, 74, 58, 0.08);
    --danger-border: rgba(185, 74, 58, 0.2);
    --info-color: var(--text-secondary);
    --info-bg: rgba(44, 42, 38, 0.04);

    /* 阴影 — 柔和自然，像卡片在桌面上的投影 */
    --shadow-xs: 0 1px 2px rgba(44, 42, 38, 0.04);
    --shadow-sm: 0 1px 4px rgba(44, 42, 38, 0.06), 0 1px 2px rgba(44, 42, 38, 0.04);
    --shadow-md: 0 4px 12px rgba(44, 42, 38, 0.07), 0 2px 4px rgba(44, 42, 38, 0.04);
    --shadow-lg: 0 10px 25px rgba(44, 42, 38, 0.08), 0 4px 10px rgba(44, 42, 38, 0.05);
    --shadow-xl: 0 20px 40px rgba(44, 42, 38, 0.1), 0 8px 20px rgba(44, 42, 38, 0.06);
    --shadow-ink: 0 4px 14px rgba(45, 106, 94, 0.15);

    /* 圆角 — 圆润柔和，像翻旧的卡片边角 */
    --radius-sm: 6px;
    --radius-md: 10px;
    --radius-lg: 14px;
    --radius-xl: 18px;

    /* 兼容旧变量名 */
    --primary-color: var(--ink);
    --secondary-color: var(--gold);

    /* 过渡 */
    --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
    --transition-fast: 0.15s var(--ease-out);
    --transition-base: 0.25s var(--ease-out);
    --transition-slow: 0.4s var(--ease-out);
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body, #app { height: 100%; width: 100%; }
  body {
    font-family: 'Georgia', 'Noto Serif SC', 'Songti SC', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', serif;
    background: var(--bg-warm);
    color: var(--text-primary); font-size: 14px; line-height: 1.6;
    -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  }

  /* 背景 — 台灯光晕：暖色光从左上角照射 */
  body::before {
    content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background:
      radial-gradient(ellipse 600px 400px at 25% 15%, rgba(196, 154, 58, 0.06) 0%, transparent 70%),
      radial-gradient(ellipse 300px 200px at 30% 20%, rgba(196, 154, 58, 0.03) 0%, transparent 100%);
  }

  /* 纸纹纹理 — 基于 CSS 的细微噪点模拟 */
  body::after {
    content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;
    opacity: 0.3;
    background-image:
      repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(44, 42, 38, 0.008) 2px, rgba(44, 42, 38, 0.008) 3px),
      repeating-linear-gradient(90deg, transparent, transparent 2px, rgba(44, 42, 38, 0.008) 2px, rgba(44, 42, 38, 0.008) 3px);
  }

  /* ===== 滚动条 ===== */
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(44, 42, 38, 0.1); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(44, 42, 38, 0.18); }

  /* ===== 布局 ===== */
  .app-layout { height: 100vh; display: flex; flex-direction: column; position: relative; z-index: 1; }

  /* ===== 头部 — 胡桃木书架质感 ===== */
  .app-header {
    height: 56px; position: relative; z-index: 10;
    display: flex; align-items: center; justify-content: space-between; padding: 0 28px;
    background: linear-gradient(135deg, #4a3d32 0%, #3d3228 50%, #4a3d32 100%);
    border-bottom: 1px solid rgba(196, 154, 58, 0.12);
    box-shadow: 0 2px 16px rgba(44, 42, 38, 0.08);
  }
  .app-header::after {
    content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent 0%, rgba(196, 154, 58, 0.15) 20%, rgba(196, 154, 58, 0.25) 50%, rgba(196, 154, 58, 0.15) 80%, transparent 100%);
  }
  .app-header .logo { display: flex; align-items: center; gap: 12px; cursor: pointer; color: #e8e0d8; position: relative; z-index: 1; }
  .app-header .logo .logo-icon {
    width: 32px; height: 32px; border-radius: 8px;
    background: rgba(232, 224, 216, 0.1);
    border: 1px solid rgba(232, 224, 216, 0.12);
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; transition: var(--transition-base);
  }
  .app-header .logo:hover .logo-icon { background: rgba(232, 224, 216, 0.18); transform: scale(1.05); }
  .app-header .logo-text { font-size: 16px; font-weight: 700; letter-spacing: 0.8px; color: #e8e0d8; }
  .app-header .header-right { display: flex; align-items: center; gap: 10px; position: relative; z-index: 1; }
  .app-header .usage-tag {
    background: rgba(232, 224, 216, 0.06); border: 1px solid rgba(232, 224, 216, 0.08);
    color: rgba(232, 224, 216, 0.75); font-size: 11px; padding: 4px 12px;
    border-radius: 20px; letter-spacing: 0.3px;
  }

  /* ===== 主体 ===== */
  .app-body { flex: 1; display: flex; overflow: hidden; }

  /* ===== 侧边栏 — 纸感书签风格 ===== */
  .app-sidebar {
    width: 220px; background: var(--bg-paper); border-right: 1px solid var(--border-default);
    flex-shrink: 0; overflow-y: auto; padding: 12px 0;
  }
  .app-sidebar .el-menu { border-right: none; background: transparent; }
  .app-sidebar .el-menu-item {
    margin: 2px 10px; border-radius: 8px; height: 44px; line-height: 44px;
    font-size: 14px; color: var(--text-secondary); transition: all var(--transition-fast);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }
  .app-sidebar .el-menu-item:hover { background: var(--ink-bg); color: var(--ink); }
  .app-sidebar .el-menu-item.is-active {
    background: var(--ink-bg);
    color: var(--ink); font-weight: 600;
    border-right: 2px solid var(--ink);
  }
  .app-sidebar .el-menu-item .el-icon { color: inherit; }
  .app-sidebar .el-menu-item.is-active .el-icon { color: var(--ink); }

  /* ===== 主内容区 ===== */
  .app-main { flex: 1; overflow-y: auto; }

  .page-container { padding: 24px 28px; max-width: 1440px; margin: 0 auto; }
  .page-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 24px; }
  .page-title {
    font-size: 22px; font-weight: 700; color: var(--text-primary); line-height: 1.3;
    display: flex; align-items: center; gap: 12px;
  }
  .page-title .title-accent {
    display: inline-block; width: 3px; height: 24px;
    background: linear-gradient(180deg, var(--ink), var(--ink-light));
    border-radius: 2px; flex-shrink: 0;
  }
  .page-subtitle { font-size: 13px; color: var(--text-tertiary); margin-top: 2px; }

  /* ===== Element Plus 卡片纸纹覆盖 ===== */

  /* 卡片 — 纸质感：纯白底色 + 柔和阴影，像纸质卡片 */
  .el-card {
    border-radius: var(--radius-lg); border: 1px solid var(--border-default);
    background: var(--bg-card); transition: all var(--transition-base);
    box-shadow: var(--shadow-xs);
  }
  .el-card:hover { box-shadow: var(--shadow-md); border-color: var(--border-strong); }
  .el-card .el-card__header {
    padding: 14px 20px; border-bottom: 1px solid var(--border-subtle);
    font-weight: 600; font-size: 14px; color: var(--text-primary);
  }
  .el-card .el-card__body { padding: 20px; }

  /* 按钮 — 圆润，像按在纸面上 */
  .el-button--primary {
    --el-button-bg-color: var(--ink);
    --el-button-border-color: var(--ink);
    --el-button-hover-bg-color: var(--ink-dark);
    --el-button-hover-border-color: var(--ink-dark);
    --el-button-active-bg-color: #1a4a40;
    --el-button-text-color: #ffffff;
    font-weight: 500;
  }
  .el-button--primary:hover { box-shadow: var(--shadow-ink); transform: translateY(-1px); }
  .el-button--primary:active { transform: translateY(0); }
  .el-button {
    border-radius: 8px; transition: all var(--transition-fast);
    font-weight: 500;
  }
  .el-button:active { transform: scale(0.97); }
  .el-button--default {
    --el-button-bg-color: transparent;
    --el-button-border-color: var(--border-strong);
    --el-button-text-color: var(--text-secondary);
    --el-button-hover-bg-color: var(--ink-bg);
    --el-button-hover-border-color: var(--ink-border);
    --el-button-hover-text-color: var(--ink);
  }

  /* 表格 — 纸感表格，像印刷在纸上 */
  .el-table {
    border-radius: var(--radius-md); overflow: hidden;
    background: transparent; color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }
  .el-table th.el-table__cell {
    background-color: var(--bg-surface); color: var(--text-secondary);
    font-weight: 600; font-size: 12px; border-bottom: 1px solid var(--border-subtle);
  }
  .el-table .el-table__body tr.el-table__row td { border-bottom: 1px solid var(--border-subtle); background: transparent; }
  .el-table .el-table__body tr:hover > td { background-color: var(--ink-bg); }
  .el-table--striped .el-table__body tr.el-table__row--striped td { background-color: rgba(44, 42, 38, 0.02); }
  .el-table .el-table__inner-wrapper::before { display: none; }
  .el-table__body-wrapper { border-radius: 0 0 var(--radius-md) var(--radius-md); }
  .el-table .cell { line-height: 1.6; }
  .el-table__empty-text { color: var(--text-tertiary); }
  .el-table__body tr.el-table__row td:first-child { border-left: 3px solid transparent; }
  .el-table__body tr:hover td:first-child { border-left-color: var(--ink-border); }

  /* 菜单 */
  .el-menu { border-right: none; }
  .el-menu-item { font-size: 14px; }

  /* 分页 — 像书签 */
  .el-pagination { font-size: 13px; }
  .el-pagination button, .el-pagination .el-pager li { background: transparent; color: var(--text-secondary); }
  .el-pagination .el-pager li.active { color: var(--ink); font-weight: 600; background: var(--ink-bg); border-radius: var(--radius-sm); }
  .el-pagination .el-pager li:hover { color: var(--ink); }
  .el-pagination button:hover { color: var(--ink); }
  .el-pagination .el-pagination__total { color: var(--text-tertiary); }
  .el-pagination .el-select .el-input__wrapper { background: var(--bg-surface); }

  /* 标签 - 柔和的彩色纸片 */
  .el-tag { border-radius: var(--radius-sm); font-weight: 500; border: 1px solid transparent; }
  .el-tag--plain { background: transparent; }
  .el-tag--success { background: var(--success-bg); border-color: var(--success-border); color: var(--success-color); }
  .el-tag--warning { background: var(--warning-bg); border-color: var(--warning-border); color: var(--warning-color); }
  .el-tag--danger { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-color); }
  .el-tag--info { background: var(--info-bg); border-color: var(--border-subtle); color: var(--text-secondary); }
  .el-tag--primary { background: var(--ink-bg); border-color: var(--ink-border); color: var(--ink); }

  /* 弹窗 */
  .el-dialog {
    border-radius: var(--radius-xl); background: var(--bg-paper);
    border: 1px solid var(--border-default);
    box-shadow: var(--shadow-xl);
  }
  .el-dialog .el-dialog__header { padding: 20px 24px 0; }
  .el-dialog .el-dialog__title { color: var(--text-primary); font-weight: 600; }
  .el-dialog .el-dialog__body { padding: 20px 24px; color: var(--text-secondary); }
  .el-dialog .el-dialog__footer { padding: 0 24px 20px; }
  .el-dialog .el-dialog__close { color: var(--text-tertiary); }
  .el-dialog .el-dialog__close:hover { color: var(--text-primary); }

  /* 输入框 — 纸面书写感 */
  .el-input__wrapper {
    border-radius: 8px; box-shadow: 0 0 0 1px var(--border-default) inset;
    background: rgba(44, 42, 38, 0.02);
  }
  .el-input__wrapper:hover { box-shadow: 0 0 0 1px var(--ink-border) inset; }
  .el-input__wrapper.is-focus { box-shadow: 0 0 0 1px var(--ink) inset, 0 0 0 3px var(--ink-bg); }
  .el-input__inner { color: var(--text-primary); background: transparent; }
  .el-input__inner::placeholder { color: var(--text-muted); }
  .el-input__prefix, .el-input__suffix { color: var(--text-tertiary); }

  /* 选择器 */
  .el-select .el-input__wrapper { border-radius: 8px; }
  .el-select-dropdown { background: var(--bg-paper); border: 1px solid var(--border-default); border-radius: var(--radius-md); box-shadow: var(--shadow-lg); }
  .el-select-dropdown__item { color: var(--text-secondary); }
  .el-select-dropdown__item.hover { background: var(--ink-bg); color: var(--ink); }
  .el-select-dropdown__item.selected { color: var(--ink); background: var(--ink-bg); }

  /* Radio 按钮 */
  .el-radio-button__inner {
    background: transparent; border-color: var(--border-strong); color: var(--text-secondary);
  }
  .el-radio-button__original-radio:checked + .el-radio-button__inner {
    background: var(--ink); border-color: var(--ink); color: #fff;
    box-shadow: -1px 0 0 0 var(--ink);
  }
  .el-radio-button__inner:hover { color: var(--ink); }
  .el-radio-button__original-radio:checked + .el-radio-button__inner:hover { color: #fff; }

  /* Radio 普通 */
  .el-radio { color: var(--text-secondary); }
  .el-radio__input.is-checked .el-radio__inner { background: var(--ink); border-color: var(--ink); }
  .el-radio__input.is-checked + .el-radio__label { color: var(--ink); }

  /* Switch */
  .el-switch__core { background: rgba(44, 42, 38, 0.15); border-color: transparent; }
  .el-switch.is-checked .el-switch__core { background-color: var(--ink); border-color: var(--ink); }

  /* Progress — 书签风格进度条 */
  .el-progress-bar__outer { background: rgba(44, 42, 38, 0.06); border-radius: 0; }
  .el-progress-bar__inner { transition: width 0.6s var(--ease-out); border-radius: 0 2px 2px 0; }
  .el-progress__text { color: var(--text-secondary); font-size: 12px; }

  /* Alert */
  .el-alert { border-radius: var(--radius-md); border: 1px solid transparent; }
  .el-alert--success { background: var(--success-bg); border-color: var(--success-border); }
  .el-alert--warning { background: var(--warning-bg); border-color: var(--warning-border); }
  .el-alert--info { background: var(--info-bg); border-color: var(--border-subtle); }
  .el-alert__title { color: var(--text-primary); }

  /* Divider */
  .el-divider { border-top-color: var(--border-subtle); }

  /* Message */
  .el-message { border-radius: var(--radius-md); border-width: 1px; }
  .el-message--success { background: var(--success-bg); border-color: var(--success-border); color: var(--success-color); }
  .el-message--warning { background: var(--warning-bg); border-color: var(--warning-border); color: var(--warning-color); }
  .el-message--error { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-color); }
  .el-message .el-message__content { font-size: 14px; color: var(--text-primary); }

  /* Message Box */
  .el-message-box { border-radius: var(--radius-xl); background: var(--bg-paper); border: 1px solid var(--border-default); box-shadow: var(--shadow-xl); }
  .el-message-box__header { padding: 20px 24px 0; }
  .el-message-box__title { color: var(--text-primary); }
  .el-message-box__content { padding: 16px 24px; color: var(--text-secondary); }
  .el-message-box__btns { padding: 0 24px 20px; }
  .el-message-box__btns .el-button--primary { background: var(--ink); border-color: var(--ink); color: #fff; font-weight: 500; }
  .el-message-box__btns .el-button--primary:hover { background: var(--ink-dark); border-color: var(--ink-dark); }

  /* Form */
  .el-form-item__label { font-weight: 500; color: var(--text-secondary); }

  /* Overlay */
  .el-overlay-dialog { display: flex; align-items: center; justify-content: center; }
  .el-overlay { background: rgba(44, 42, 38, 0.3); }

  /* Loading */
  .el-loading-spinner .path { stroke: var(--ink); }
  .el-loading-spinner .el-loading-text { color: var(--ink); }

  /* Select caret */
  .el-select .el-select__caret { color: var(--text-tertiary); }

  /* Checkbox */
  .el-checkbox__inner { background: transparent; border-color: var(--border-strong); }
  .el-checkbox__input.is-checked .el-checkbox__inner { background: var(--ink); border-color: var(--ink); }
  .el-checkbox__input.is-indeterminate .el-checkbox__inner { background: var(--ink); border-color: var(--ink); }

  /* ===== 单词高亮 — 荧光笔效果 ===== */
  .word-hit {
    color: #1a5a4a; font-weight: 600;
    background: linear-gradient(180deg, transparent 55%, var(--marker-yellow-bg) 55%);
    padding: 0 3px; border-radius: 2px;
  }
  .word-miss {
    color: #8a2a1a; font-weight: 600;
    background: linear-gradient(180deg, transparent 55%, rgba(185, 74, 58, 0.1) 55%);
    padding: 0 3px; border-radius: 2px;
  }

  /* ===== 空状态 ===== */
  .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 60px 20px; color: var(--text-tertiary); }
  .empty-state .el-icon { font-size: 48px; margin-bottom: 16px; opacity: 0.4; }

  /* ===== 过渡动画 — 纸片滑动 ===== */
  .fade-enter-active, .fade-leave-active { transition: opacity 0.25s var(--ease-out), transform 0.25s var(--ease-out); }
  .fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(8px); }

  /* ===== 工具类 ===== */
  .pagination-wrapper { display: flex; justify-content: flex-end; margin-top: 20px; }
  .card-title { font-weight: 600; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; color: var(--text-primary); }
  .word-tags { display: flex; flex-wrap: wrap; gap: 8px; }
  .word-tag-item { border-radius: var(--radius-sm); font-size: 14px; }
  .flex { display: flex; }
  .flex-center { display: flex; align-items: center; justify-content: center; }
  .flex-between { display: flex; align-items: center; justify-content: space-between; }
  .gap-sm { gap: 8px; }
  .gap-md { gap: 16px; }
  .mb-sm { margin-bottom: 8px; }
  .mb-md { margin-bottom: 16px; }
  .mt-md { margin-top: 16px; }
  .ml-sm { margin-left: 8px; }
  .ml-md { margin-left: 16px; }
  .text-secondary { color: var(--text-secondary); }
  .text-success { color: var(--success-color); }
  .text-danger { color: var(--danger-color); }
  .rotating { animation: rotating 1.5s linear infinite; }
  @keyframes rotating { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

  /* ===== 音频播放器 — 卡片式 ===== */
  .audio-player {
    display: flex; align-items: center; gap: 16px; padding: 14px 18px;
    background: var(--ink-bg); border: 1px solid var(--ink-border);
    border-radius: var(--radius-lg);
  }
  .audio-player .progress-section { flex: 1; min-width: 200px; }
  .audio-player .time-display { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-tertiary); margin-top: -4px; }
  .audio-player .control-group { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); }
  .audio-player .control-label { font-size: 12px; white-space: nowrap; }
  .audio-player audio { width: 100%; border-radius: var(--radius-md); }
  .audio-player audio::-webkit-media-controls-panel { background: var(--bg-card); }

  /* ===== 正文 — 书本式排版 ===== */
  .english-body {
    white-space: pre-wrap; line-height: 2.2; font-size: 15px; color: var(--text-primary);
    font-family: 'Georgia', 'Noto Serif SC', 'Songti SC', serif;
    letter-spacing: 0.02em;
  }
  .chinese-body {
    white-space: pre-wrap; line-height: 2; font-size: 14px; color: var(--text-secondary);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif;
  }

  /* ===== 单词列表 ===== */
  .word-list { display: flex; flex-wrap: wrap; gap: 8px; }
  .word-list-tag { font-size: 14px; padding: 4px 12px; }

  /* ===== 词伙搭配 — 金色标签 ===== */
  .collocations-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
  .collocation-item {
    display: flex; align-items: center; gap: 8px; padding: 10px 14px;
    background: var(--gold-bg); border: 1px solid var(--gold-border);
    border-radius: var(--radius-md); font-size: 14px; transition: var(--transition-fast);
    color: var(--text-secondary);
  }
  .collocation-item:hover { border-color: var(--gold); }

  /* ===== 操作按钮 ===== */
  .action-buttons { display: flex; gap: 12px; flex-wrap: wrap; }

  /* ===== 文本列表 — 卡片列表 ===== */
  .text-list { display: flex; flex-direction: column; gap: 12px; }
  .text-item {
    padding: 18px 20px; border: 1px solid var(--border-default); border-radius: var(--radius-lg);
    cursor: pointer; transition: all var(--transition-base);
    background: var(--bg-card);
  }
  .text-item:hover { border-color: var(--ink-border); box-shadow: var(--shadow-ink); transform: translateY(-1px); }
  .text-item:active { transform: translateY(0); }
  .text-item-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .text-item-title { font-size: 15px; font-weight: 600; color: var(--text-primary); }
  .text-item-tags { display: flex; gap: 6px; flex-wrap: wrap; }
  .text-item-preview {
    font-size: 13px; color: var(--text-tertiary); line-height: 1.7; margin-bottom: 12px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .text-item-footer { display: flex; align-items: center; justify-content: space-between; }
  .text-item-footer .word-stats .el-tag--success { background: var(--success-bg); border-color: var(--success-border); color: var(--success-color); }
  .text-item-footer .word-stats .el-tag--danger { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-color); }

  .text-header { display: flex; flex-direction: column; gap: 12px; }
  .text-title { font-size: 20px; font-weight: 700; color: var(--text-primary); }
  .text-meta { display: flex; flex-wrap: wrap; gap: 8px; }

  /* ===== 统计卡片 ===== */
  .stats-row { margin-bottom: 20px; }
  .stat-item {
    text-align: center; padding: 16px; border-radius: var(--radius-lg);
    border: 1px solid transparent; transition: all var(--transition-base);
  }
  .stat-item:hover { transform: translateY(-2px); box-shadow: var(--shadow-md); }
  .stat-item.success { background: var(--success-bg); border-color: var(--success-border); }
  .stat-item.warning { background: var(--warning-bg); border-color: var(--warning-border); }
  .stat-item.danger { background: var(--danger-bg); border-color: var(--danger-border); }
  .stat-num { display: block; font-size: 32px; font-weight: 800; line-height: 1.2; }
  .stat-item.success .stat-num { color: var(--success-color); }
  .stat-item.warning .stat-num { color: var(--warning-color); }
  .stat-item.danger .stat-num { color: var(--danger-color); }
  .stat-label { font-size: 12px; color: var(--text-tertiary); margin-top: 4px; display: block; }

  .section-label { font-size: 12px; font-weight: 600; color: var(--text-tertiary); display: block; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
  .word-preview .word-tags { max-height: 200px; overflow-y: auto; }

  /* ===== 熟词生意库 ===== */
  .polysemy-card .card-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  .polysemy-card .word-title { font-size: 20px; font-weight: 700; color: var(--text-primary); }
  .polysemy-card .meanings-section { display: flex; flex-direction: column; gap: 8px; }
  .polysemy-card .meaning-row { padding: 14px 16px; border-radius: var(--radius-md); border: 1px solid transparent; }
  .polysemy-card .meaning-row.common { background: var(--info-bg); border-color: var(--border-subtle); }
  .polysemy-card .meaning-row.business { background: var(--ink-bg); border-color: var(--ink-border); }
  .polysemy-card .meaning-label { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; color: var(--text-tertiary); margin-bottom: 6px; }
  .polysemy-card .meaning-en { font-size: 15px; font-weight: 500; color: var(--text-primary); }
  .polysemy-card .meaning-zh { font-size: 14px; color: var(--text-tertiary); margin-top: 2px; }
  .polysemy-card .meaning-arrow { text-align: center; padding: 4px 0; }
  .polysemy-card .collocations-list { display: flex; flex-wrap: wrap; gap: 8px; }
  .polysemy-card .collocations-list .el-tag { background: var(--gold-bg); border-color: var(--gold-border); color: var(--gold); }
  .polysemy-card .example-en { font-size: 14px; line-height: 1.8; color: var(--text-primary); }
  .polysemy-card .example-zh { font-size: 14px; color: var(--text-tertiary); margin-top: 4px; }

  /* ===== 页面内容入场动画 — 纸片落下 ===== */
  @keyframes pageEnter {
    from { opacity: 0; transform: translateY(16px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .page-container { animation: pageEnter 0.35s var(--ease-out); }

  /* ===== 结果页高亮词条 ===== */
  .el-card .word-list .el-tag--success { background: var(--success-bg); border-color: var(--success-border); color: var(--success-color); }
  .el-card .word-list .el-tag--danger { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-color); }

  /* ===== 收藏按钮 ===== */
  .el-button--warning {
    --el-button-bg-color: var(--gold-bg);
    --el-button-border-color: var(--gold-border);
    --el-button-text-color: var(--gold);
    --el-button-hover-bg-color: rgba(196, 154, 58, 0.15);
    --el-button-hover-border-color: var(--gold);
  }

  /* ===== 编译配置页 ===== */
  .el-form-item__label { font-weight: 500; color: var(--text-secondary); }

  /* ===== 表格内下拉 ===== */
  .el-table .el-select .el-input__wrapper { background: transparent; box-shadow: none; }
  .el-table .el-select .el-input__wrapper:hover { box-shadow: 0 0 0 1px var(--ink-border) inset; }

  /* ===== Progress 在表格内 ===== */
  .el-progress { background: transparent; }
</style>
</head>
<body>
<div id="app">
  <div class="app-layout">
    <!-- 顶部栏 -->
    <header class="app-header">
      <div class="logo" @click="currentPage='words'">
        <div class="logo-icon">
          <el-icon :size="18" color="#fff"><Reading /></el-icon>
        </div>
        <span class="logo-text">TOEIC 顽固词深度加工系统</span>
      </div>
      <div class="header-right">
        <span class="usage-tag">
          AI {{ dailyUsage.ai }}/{{ dailyUsage.ai_limit }} · TTS {{ dailyUsage.tts }}/{{ dailyUsage.tts_limit }}
        </span>
      </div>
    </header>

    <div class="app-body">
      <!-- 侧边栏 -->
      <aside class="app-sidebar">
        <el-menu :default-active="currentPage" @select="handleMenuSelect">
          <el-menu-item index="words">
            <el-icon><Collection /></el-icon>
            <span>单词库</span>
          </el-menu-item>
          <el-menu-item index="import">
            <el-icon><Upload /></el-icon>
            <span>导入单词</span>
          </el-menu-item>
          <el-menu-item index="compile">
            <el-icon><MagicStick /></el-icon>
            <span>编译配置</span>
          </el-menu-item>
          <el-menu-item index="history">
            <el-icon><Document /></el-icon>
            <span>历史记录</span>
          </el-menu-item>
          <el-menu-item index="polysemy">
            <el-icon><Discount /></el-icon>
            <span>熟词生意库</span>
          </el-menu-item>
        </el-menu>
      </aside>

      <!-- 主内容 -->
      <main class="app-main">
        <!-- 单词库 -->
        <div v-if="currentPage==='words'" class="page-container">
          <div class="page-header">
            <div>
              <h2 class="page-title"><span class="title-accent"></span>单词库</h2>
              <p class="page-subtitle">管理你的托业顽固词汇</p>
            </div>
            <div class="flex gap-sm">
              <el-button :icon="Upload" @click="currentPage='import'">导入单词</el-button>
              <el-button type="primary" :icon="Plus" @click="showAddWordDialog=true">添加单词</el-button>
            </div>
          </div>
          <el-card shadow="never" class="mb-md">
            <div class="flex-between">
              <el-input v-model="wordSearch" placeholder="搜索单词..." :prefix-icon="Search" clearable style="width:300px" @clear="loadWords" @keyup.enter="loadWords"></el-input>
              <div class="flex gap-sm">
                <el-tag v-if="selectedWords.length>0" type="primary" size="large">已选 {{ selectedWords.length }} 个单词</el-tag>
                <el-button v-if="selectedWords.length>0" type="primary" :icon="MagicStick" @click="goCompile">发起编译</el-button>
              </div>
            </div>
          </el-card>
          <el-card shadow="never">
            <el-table :data="wordsData" v-loading="wordsLoading" @selection-change="s=>selectedWords=s" row-key="id" stripe>
              <el-table-column type="selection" width="50"></el-table-column>
              <el-table-column label="单词" min-width="140">
                <template #default="{row}">
                  <div class="flex gap-sm" style="align-items:center">
                    <span style="font-weight:600">{{ row.word }}</span>
                    <el-tag v-if="row.has_polysemy" size="small" type="warning" effect="plain">熟词生意</el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="source" label="来源" width="100">
                <template #default="{row}"><span class="text-secondary">{{ row.source || '-' }}</span></template>
              </el-table-column>
              <el-table-column label="难度" width="100">
                <template #default="{row}">
                  <el-select v-model="row.difficulty" size="small" @change="v=>updateWord(row,'difficulty',v)">
                    <el-option label="初级" value="beginner"></el-option>
                    <el-option label="中级" value="intermediate"></el-option>
                    <el-option label="高级" value="advanced"></el-option>
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column label="顽固度" width="120">
                <template #default="{row}">
                  <el-progress :percentage="row.stubborn_score||0" :color="row.stubborn_score>=80?'#dc2626':row.stubborn_score>=50?'#d97706':'#16a34a'" :stroke-width="8" />
                </template>
              </el-table-column>
              <el-table-column label="状态" width="120">
                <template #default="{row}">
                  <el-select v-model="row.status" size="small" @change="v=>updateWord(row,'status',v)">
                    <el-option label="新词" value="new"></el-option>
                    <el-option label="学习中" value="learning"></el-option>
                    <el-option label="已掌握" value="mastered"></el-option>
                    <el-option label="已放弃" value="abandoned"></el-option>
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column label="创建时间" width="160">
                <template #default="{row}"><span class="text-secondary">{{ formatDate(row.created_at) }}</span></template>
              </el-table-column>
              <el-table-column label="操作" width="140" fixed="right">
                <template #default="{row}">
                  <el-button link type="primary" :icon="EditPen" @click="editNote(row)">备注</el-button>
                  <el-button link type="danger" :icon="Delete" @click="deleteWord(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <div class="pagination-wrapper">
              <el-pagination v-model:current-page="wordPage" v-model:page-size="wordPageSize" :total="wordTotal" :page-sizes="[10,20,50,100]" layout="total, sizes, prev, pager, next" @size-change="loadWords" @current-change="loadWords"></el-pagination>
            </div>
          </el-card>
        </div>

        <!-- 导入单词 -->
        <div v-if="currentPage==='import'" class="page-container">
          <div class="page-header">
            <div>
              <h2 class="page-title"><span class="title-accent"></span>导入单词</h2>
              <p class="page-subtitle">批量导入托业顽固词汇，支持换行或逗号分隔</p>
            </div>
            <el-button :icon="Back" @click="currentPage='words'">返回单词库</el-button>
          </div>
          <el-row :gutter="24">
            <el-col :span="14">
              <el-card shadow="never">
                <template #header>
                  <div class="flex-between">
                    <span class="card-title">输入单词</span>
                    <el-button :icon="UploadFilled" size="small" @click="triggerFileInput">从文件导入 (txt/csv)</el-button>
                    <input ref="fileInputRef" type="file" accept=".txt,.csv" style="display:none" @change="handleFileChange" />
                  </div>
                </template>
                <el-input v-model="importText" type="textarea" :rows="12" placeholder="请输入单词，支持换行分隔或逗号分隔，例如：&#10;accommodate&#10;negotiate, delegate&#10;procurement"></el-input>
                <div class="flex gap-sm mt-md">
                  <el-button type="primary" :icon="Search" :loading="parsing" @click="handleParse">预览解析</el-button>
                  <el-button :icon="RefreshLeft" @click="importText=''">清空</el-button>
                </div>
              </el-card>
            </el-col>
            <el-col :span="10">
              <el-card shadow="never" style="min-height:400px">
                <template #header><span class="card-title">解析结果</span></template>
                <div v-if="!parseResult" class="empty-state">
                  <el-icon><DocumentRemove /></el-icon>
                  <p>输入单词后点击"预览解析"查看结果</p>
                </div>
                <div v-else class="parse-result">
                  <el-row :gutter="16" class="stats-row">
                    <el-col :span="8">
                      <div class="stat-item success">
                        <span class="stat-num">{{ parseResult.words.length }}</span>
                        <span class="stat-label">有效单词</span>
                      </div>
                    </el-col>
                    <el-col :span="8">
                      <div class="stat-item warning">
                        <span class="stat-num">{{ parseResult.duplicate_count }}</span>
                        <span class="stat-label">重复单词</span>
                      </div>
                    </el-col>
                    <el-col :span="8">
                      <div class="stat-item danger">
                        <span class="stat-num">{{ parseResult.invalid_count }}</span>
                        <span class="stat-label">无效单词</span>
                      </div>
                    </el-col>
                  </el-row>
                  <el-alert v-if="parseResult.polysemy_matched>0" type="success" :closable="false" class="mb-md">
                    <template #title>发现 {{ parseResult.polysemy_matched }} 个熟词生意匹配</template>
                  </el-alert>
                  <div class="word-preview">
                    <span class="section-label">有效单词预览</span>
                    <div class="word-tags">
                      <el-tag v-for="w in parseResult.words" :key="w" class="word-tag-item" effect="plain">{{ w }}</el-tag>
                    </div>
                  </div>
                  <el-button type="primary" size="large" style="width:100%;margin-top:16px" :loading="importing" :disabled="!parseResult.words.length" @click="doImport">
                    确认导入 ({{ parseResult.words.length }} 个单词)
                  </el-button>
                </div>
              </el-card>
            </el-col>
          </el-row>
        </div>

        <!-- 编译配置 -->
        <div v-if="currentPage==='compile'" class="page-container">
          <div class="page-header">
            <div>
              <h2 class="page-title"><span class="title-accent"></span>编译配置</h2>
              <p class="page-subtitle">设置 AI 编译参数，将单词编译为商务语境文本</p>
            </div>
            <el-button :icon="Back" @click="currentPage='words'">返回单词库</el-button>
          </div>
          <el-card shadow="never" class="mb-md">
            <template #header>
              <div class="flex-between">
                <span class="card-title">已选单词 <el-tag type="primary" size="small" class="ml-sm">{{ compileWords.length }} 个</el-tag></span>
                <el-button link type="primary" @click="currentPage='words'">修改选择</el-button>
              </div>
            </template>
            <div v-if="compileWords.length===0" class="empty-state">
              <el-icon><WarningFilled /></el-icon>
              <p>未选择任何单词，请返回单词库选择</p>
              <el-button type="primary" class="mt-md" @click="currentPage='words'">去选择单词</el-button>
            </div>
            <div v-else class="word-tags">
              <el-tag v-for="w in compileWords" :key="w.id" closable @close="compileWords=compileWords.filter(x=>x.id!==w.id)" class="word-tag-item">{{ w.word }}</el-tag>
            </div>
          </el-card>
          <el-card shadow="never" v-if="compileWords.length>0">
            <el-form label-width="120px" label-position="right">
              <el-form-item label="内容形式">
                <el-radio-group v-model="compileForm.content_form">
                  <el-radio-button v-for="cf in CONTENT_FORMS" :key="cf.value" :value="cf.value">{{ cf.label }}</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="场景类型">
                <el-select v-model="compileForm.scene_type" placeholder="选择场景" style="width:240px">
                  <el-option v-for="st in SCENE_TYPES" :key="st.value" :label="st.label" :value="st.value"></el-option>
                </el-select>
              </el-form-item>
              <el-form-item label="难度等级">
                <el-radio-group v-model="compileForm.difficulty">
                  <el-radio-button v-for="d in DIFFICULTY_LEVELS" :key="d.value" :value="d.value">{{ d.label }}</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="文本长度">
                <el-radio-group v-model="compileForm.length_level">
                  <el-radio v-for="l in LENGTH_LEVELS" :key="l.value" :value="l.value">{{ l.label }}</el-radio>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="中文翻译">
                <el-switch v-model="compileForm.include_translation" active-text="包含中文翻译"></el-switch>
              </el-form-item>
              <el-form-item label="立即生成音频">
                <el-switch v-model="compileForm.generate_audio" active-text="编译完成后自动生成 TTS 听力音频"></el-switch>
              </el-form-item>
              <el-form-item v-if="compileForm.generate_audio" label="TTS 模型">
                <el-select v-model="compileForm.tts_model" placeholder="选择语音合成模型" style="width:460px">
                  <el-option-group v-for="(items, label) in groupedTtsModels" :key="label" :label="label">
                    <el-option v-for="m in items" :key="m.value" :label="m.label" :value="m.value">
                      <span style="font-size:13px">{{ m.label }}</span>
                      <br><span style="font-size:11px;color:var(--text-tertiary)">推荐音色: {{ m.voices }}</span>
                    </el-option>
                  </el-option-group>
                </el-select>
                <span v-if="compileForm.tts_model" class="ml-sm" style="font-size:12px;color:var(--text-tertiary)">
                  <br>当前默认音色: {{ getModelDefaultVoice(compileForm.tts_model) }}
                </span>
              </el-form-item>
              <el-divider></el-divider>
              <el-form-item>
                <el-button type="primary" size="large" :icon="MagicStick" :loading="compiling" @click="handleCompile">开始 AI 编译</el-button>
                <span class="ml-md text-secondary" style="font-size:13px">编译过程约需 10-30 秒，请耐心等待</span>
              </el-form-item>
            </el-form>
          </el-card>
        </div>

        <!-- 历史记录 -->
        <div v-if="currentPage==='history'" class="page-container">
          <div class="page-header">
            <div>
              <h2 class="page-title"><span class="title-accent"></span>历史记录</h2>
              <p class="page-subtitle">查看所有编译生成的文本</p>
            </div>
          </div>
          <el-card shadow="never" class="mb-md">
            <div class="flex gap-md" style="flex-wrap:wrap">
              <el-select v-model="historyFilter" placeholder="内容形式" clearable style="width:160px">
                <el-option v-for="cf in CONTENT_FORMS" :key="cf.value" :label="cf.label" :value="cf.value"></el-option>
              </el-select>
              <el-button :icon="Search" @click="loadHistory">查询</el-button>
            </div>
          </el-card>
          <el-card shadow="never" v-loading="historyLoading">
            <div v-if="historyData.length===0 && !historyLoading" class="empty-state">
              <el-icon><DocumentRemove /></el-icon>
              <p>暂无历史记录</p>
            </div>
            <div v-else class="text-list">
              <div v-for="h in historyData" :key="h.id" class="text-item" @click="viewResult(h)">
                <div class="text-item-header">
                  <span class="text-item-title">{{ h.title || 'Untitled' }}</span>
                  <div class="text-item-tags">
                    <el-tag size="small">{{ contentFormText(h.content_form) }}</el-tag>
                    <el-tag size="small" type="info">{{ sceneTypeText(h.scene_type) }}</el-tag>
                    <el-tag v-if="h.is_favorited" size="small" type="warning" :icon="StarFilled">已收藏</el-tag>
                  </div>
                </div>
                <p class="text-item-preview">{{ (h.body_en||'').slice(0,150) }}{{ (h.body_en||'').length>150?'...':'' }}</p>
                <div class="text-item-footer">
                  <div class="word-stats flex gap-sm">
                    <el-tag size="small" type="success" effect="plain">命中 {{ (h.included_words||[]).length }}</el-tag>
                    <el-tag v-if="(h.missing_words||[]).length>0" size="small" type="danger" effect="plain">未命中 {{ (h.missing_words||[]).length }}</el-tag>
                  </div>
                  <span class="text-secondary">{{ formatDate(h.created_at) }}</span>
                </div>
              </div>
            </div>
            <div class="pagination-wrapper">
              <el-pagination v-model:current-page="historyPage" :page-size="20" :total="historyTotal" layout="total, prev, pager, next" @current-change="loadHistory"></el-pagination>
            </div>
          </el-card>
        </div>

        <!-- 熟词生意库 -->
        <div v-if="currentPage==='polysemy'" class="page-container">
          <div class="page-header">
            <div>
              <h2 class="page-title"><span class="title-accent"></span>熟词生意库</h2>
              <p class="page-subtitle">托业高频"熟词生意"词汇，普通含义与商务含义对比</p>
            </div>
          </div>
          <el-card shadow="never" class="mb-md">
            <div class="flex gap-md">
              <el-input v-model="polysemySearch" placeholder="搜索单词，如：accommodate, address" :prefix-icon="Search" clearable style="width:360px" @keyup.enter="searchPolysemy"></el-input>
              <el-button type="primary" :icon="Search" @click="searchPolysemy">搜索</el-button>
            </div>
          </el-card>
          <el-card v-if="polysemyResult!==undefined" shadow="never" class="mb-md">
            <template #header><span class="card-title">搜索结果</span></template>
            <div v-if="polysemyResult">
              <div class="polysemy-card">
                <div class="card-header">
                  <span class="word-title">{{ polysemyResult.word }}</span>
                  <div class="flex gap-sm">
                    <el-tag v-if="polysemyResult.frequency_level" size="small" type="warning">频率: {{ polysemyResult.frequency_level }}</el-tag>
                    <el-tag v-if="polysemyResult.toc_part" size="small">{{ polysemyResult.toc_part }}</el-tag>
                  </div>
                </div>
                <div class="meanings-section" style="margin-top:12px">
                  <div class="meaning-row common">
                    <div class="meaning-label"><el-icon><Sunny /></el-icon> 普通含义</div>
                    <div class="meaning-en">{{ polysemyResult.common_meaning_en }}</div>
                    <div class="meaning-zh">{{ polysemyResult.common_meaning_zh }}</div>
                  </div>
                  <div class="meaning-arrow"><el-icon color="var(--primary-color)"><Bottom /></el-icon></div>
                  <div class="meaning-row business">
                    <div class="meaning-label"><el-icon color="var(--secondary-color)"><Briefcase /></el-icon> <span style="color:var(--secondary-color)">托业含义</span></div>
                    <div class="meaning-en">{{ polysemyResult.business_meaning_en }}</div>
                    <div class="meaning-zh">{{ polysemyResult.business_meaning_zh }}</div>
                  </div>
                </div>
                <div v-if="polysemyResult.collocations&&polysemyResult.collocations.length" style="margin-top:12px">
                  <span class="section-label">常用搭配</span>
                  <div class="collocations-list">
                    <el-tag v-for="c in polysemyResult.collocations" :key="c" effect="plain">{{ c }}</el-tag>
                  </div>
                </div>
                <div v-if="polysemyResult.example_en" style="margin-top:12px">
                  <span class="section-label">托业例句</span>
                  <p class="example-en">{{ polysemyResult.example_en }}</p>
                  <p class="example-zh">{{ polysemyResult.example_zh }}</p>
                </div>
              </div>
            </div>
            <div v-else class="empty-state">
              <el-icon><Search /></el-icon>
              <p>未找到该单词的熟词生意信息</p>
            </div>
          </el-card>
          <el-card shadow="never">
            <template #header><span class="card-title">高频熟词生意</span></template>
            <div v-loading="polysemyLoading">
              <div v-if="polysemyHot.length===0 && !polysemyLoading" class="empty-state">
                <el-icon><DocumentRemove /></el-icon>
                <p>暂无数据</p>
              </div>
              <el-row v-else :gutter="16">
                <el-col v-for="entry in polysemyHot" :key="entry.id" :span="12" class="mb-md">
                  <div class="polysemy-card">
                    <div class="card-header">
                      <span class="word-title">{{ entry.word }}</span>
                      <div class="flex gap-sm">
                        <el-tag v-if="entry.frequency_level" size="small" type="warning">频率: {{ entry.frequency_level }}</el-tag>
                        <el-tag v-if="entry.toc_part" size="small">{{ entry.toc_part }}</el-tag>
                      </div>
                    </div>
                    <div class="meanings-section" style="margin-top:12px">
                      <div class="meaning-row common">
                        <div class="meaning-label"><el-icon><Sunny /></el-icon> 普通含义</div>
                        <div class="meaning-en">{{ entry.common_meaning_en }}</div>
                        <div class="meaning-zh">{{ entry.common_meaning_zh }}</div>
                      </div>
                      <div class="meaning-arrow"><el-icon color="var(--primary-color)"><Bottom /></el-icon></div>
                      <div class="meaning-row business">
                        <div class="meaning-label"><el-icon color="var(--secondary-color)"><Briefcase /></el-icon> <span style="color:var(--secondary-color)">托业含义</span></div>
                        <div class="meaning-en">{{ entry.business_meaning_en }}</div>
                        <div class="meaning-zh">{{ entry.business_meaning_zh }}</div>
                      </div>
                    </div>
                  </div>
                </el-col>
              </el-row>
              <div class="pagination-wrapper">
                <el-pagination v-model:current-page="polysemyPage" :page-size="20" :total="polysemyTotal" layout="total, prev, pager, next" @current-change="loadPolysemyHot"></el-pagination>
              </div>
            </div>
          </el-card>
        </div>

        <!-- 结果详情页 -->
        <div v-if="currentPage==='result'" class="page-container">
          <div class="page-header">
            <div>
              <h2 class="page-title"><span class="title-accent"></span>编译结果</h2>
              <p class="page-subtitle">AI 生成的商务语境文本与听力音频</p>
            </div>
            <el-button :icon="Back" @click="currentPage='history'">返回历史记录</el-button>
          </div>
          <div v-if="resultLoading" class="flex-center" style="padding:100px 0">
            <el-icon class="rotating" :size="32" color="var(--primary-color)"><Loading /></el-icon>
            <span class="ml-sm text-secondary">正在加载任务信息...</span>
          </div>
          <template v-else-if="resultData">
            <el-card shadow="never" class="mb-md">
              <div class="text-header">
                <h3 class="text-title">{{ resultData.title || 'Untitled' }}</h3>
                <div class="text-meta">
                  <el-tag size="small">{{ contentFormText(resultData.content_form) }}</el-tag>
                  <el-tag size="small" type="info">{{ sceneTypeText(resultData.scene_type) }}</el-tag>
                  <el-tag size="small" type="warning">{{ difficultyText(resultData.difficulty) }}</el-tag>
                  <el-tag v-for="tag in (resultData.toc_part_tags||[])" :key="tag" size="small" type="success" effect="plain">{{ tag }}</el-tag>
                </div>
              </div>
            </el-card>
            <el-card shadow="never" class="mb-md">
              <template #header><span class="card-title">英文正文</span></template>
              <div class="english-body" v-html="highlightedResult"></div>
            </el-card>
            <el-card v-if="resultData.body_zh" shadow="never" class="mb-md">
              <template #header>
                <div class="flex-between">
                  <span class="card-title">中文翻译</span>
                  <el-button link :icon="showZh?ArrowUp:ArrowDown" @click="showZh=!showZh">{{ showZh?'收起':'展开' }}</el-button>
                </div>
              </template>
              <div v-show="showZh" class="chinese-body">{{ resultData.body_zh }}</div>
            </el-card>
            <el-row :gutter="16" class="mb-md">
              <el-col :span="12">
                <el-card shadow="never">
                  <template #header><span class="card-title text-success"><el-icon><CircleCheckFilled /></el-icon> 命中单词 ({{ (resultData.included_words||[]).length }})</span></template>
                  <div class="word-list">
                    <el-tag v-for="w in (resultData.included_words||[])" :key="w" type="success" effect="plain" class="word-list-tag">{{ w }}</el-tag>
                    <span v-if="!resultData.included_words||resultData.included_words.length===0" class="text-secondary">无</span>
                  </div>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card shadow="never">
                  <template #header><span class="card-title text-danger"><el-icon><CircleCloseFilled /></el-icon> 未命中单词 ({{ (resultData.missing_words||[]).length }})</span></template>
                  <div class="word-list">
                    <el-tag v-for="w in (resultData.missing_words||[])" :key="w" type="danger" effect="plain" class="word-list-tag">{{ w }}</el-tag>
                    <span v-if="!resultData.missing_words||resultData.missing_words.length===0" class="text-secondary">全部命中</span>
                  </div>
                </el-card>
              </el-col>
            </el-row>
            <el-card v-if="resultData.collocations&&resultData.collocations.length>0" shadow="never" class="mb-md">
              <template #header><span class="card-title">词伙搭配</span></template>
              <div class="collocations-grid">
                <div v-for="c in resultData.collocations" :key="c" class="collocation-item">
                  <el-icon color="var(--secondary-color)"><Connection /></el-icon>
                  <span>{{ c }}</span>
                </div>
              </div>
            </el-card>
            <el-card shadow="never" class="mb-md">
              <template #header>
                <div class="flex-between">
                  <span class="card-title"><el-icon><Headset /></el-icon> 听力音频</span>
                  <span v-if="resultData.tts_model" class="text-secondary" style="font-size:12px">模型: {{ resultData.tts_model }}</span>
                </div>
              </template>
              <div v-if="resultData.audio_url" class="audio-player">
                <audio :src="resultData.audio_url" controls style="width:100%"></audio>
              </div>
              <div v-else>
                <el-alert v-if="resultData.audio_error" type="error" :closable="false" class="mb-sm" show-icon>
                  <template #title>{{ resultData.audio_error }}</template>
                </el-alert>
              </div>
              <div class="mt-md flex gap-sm" style="align-items:center;flex-wrap:wrap">
                <el-select v-model="regenerateTtsModel" placeholder="选择模型" style="width:400px" size="small">
                  <el-option-group v-for="(items, label) in groupedTtsModels" :key="label" :label="label">
                    <el-option v-for="m in items" :key="m.value" :label="m.label" :value="m.value">
                      <span style="font-size:12px">{{ m.label }}</span>
                      <br><span style="font-size:10px;color:var(--text-tertiary)">推荐音色: {{ m.voices }}</span>
                    </el-option>
                  </el-option-group>
                </el-select>
                <span style="font-size:12px;color:var(--text-tertiary)">当前音色: {{ getModelDefaultVoice(regenerateTtsModel) }}</span>
                <el-button type="primary" :icon="Headset" :loading="audioLoading" @click="regenerateAudio">{{ resultData.audio_url ? '重新生成' : '生成音频' }}</el-button>
              </div>
            </el-card>
            <el-card shadow="never">
              <div class="action-buttons">
                <el-button :type="resultData.is_favorited?'warning':'default'" :icon="resultData.is_favorited?StarFilled:Star" @click="toggleFavorite">{{ resultData.is_favorited?'已收藏':'收藏' }}</el-button>
              </div>
            </el-card>
          </template>
        </div>
      </main>
    </div>
  </div>

  <!-- 添加单词对话框 -->
  <el-dialog v-model="showAddWordDialog" title="添加单词" width="480px" @close="addWordForm.word='';addWordForm.source=''">
    <el-form label-width="80px">
      <el-form-item label="单词">
        <el-input v-model="addWordForm.word" placeholder="请输入英文单词" clearable @keyup.enter="submitAddWord"></el-input>
      </el-form-item>
      <el-form-item label="来源">
        <el-input v-model="addWordForm.source" placeholder="可选，如：真题、教材"></el-input>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="showAddWordDialog=false">取消</el-button>
      <el-button type="primary" :loading="addWordLoading" @click="submitAddWord">添加</el-button>
    </template>
  </el-dialog>

  <!-- 备注对话框 -->
  <el-dialog v-model="showNoteDialog" title="编辑备注" width="480px">
    <el-form label-width="60px">
      <el-form-item label="单词"><span style="font-weight:600">{{ noteWord?.word }}</span></el-form-item>
      <el-form-item label="备注">
        <el-input v-model="noteText" type="textarea" :rows="4" placeholder="输入备注信息，如记忆技巧、易混淆点等"></el-input>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="showNoteDialog=false">取消</el-button>
      <el-button type="primary" :loading="noteLoading" @click="saveNote">保存</el-button>
    </template>
  </el-dialog>
</div>

<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<script src="https://unpkg.com/element-plus"></script>
<script src="https://unpkg.com/@element-plus/icons-vue"></script>
<script>
const {createApp, ref, reactive, computed, watch, nextTick, onMounted} = Vue
const ElIcons = ElementPlusIconsVue

const app = createApp({
  setup() {
    // 图标注册
    for (const [k,v] of Object.entries(ElIcons)) {
      app.component(k, v)
    }

    const currentPage = ref('words')
    const dailyUsage = ref({ai:0, tts:0, ai_limit:20, tts_limit:50})

    // 常量
    const CONTENT_FORMS = [
      {value:'dialogue',label:'商务对话'},
      {value:'email',label:'邮件'},
      {value:'memo',label:'备忘录'},
      {value:'report',label:'报告摘要'},
      {value:'short_narrative',label:'短叙述'},
    ]
    const SCENE_TYPES = [
      {value:'meeting',label:'会议'},{value:'travel',label:'差旅'},
      {value:'procurement',label:'采购'},{value:'hr',label:'HR'},
      {value:'finance',label:'财务'},{value:'customer_service',label:'客户服务'},
      {value:'project_management',label:'项目管理'},{value:'marketing',label:'市场推广'},
    ]
    const DIFFICULTY_LEVELS = [
      {value:'beginner',label:'初级'},{value:'intermediate',label:'中级'},{value:'advanced',label:'高级'},
    ]
    const LENGTH_LEVELS = [
      {value:'short',label:'短篇 (约80词)'},{value:'medium',label:'中篇 (约150词)'},{value:'long',label:'长篇 (约250词)'},
    ]
    const TTS_MODELS = [
      {value:'qwen-audio-3.0-tts-plus',label:'Qwen-Audio TTS Plus (最佳音质·48kHz·指令控制)',group:'Qwen-Audio-TTS 系列',voices:'loongmary(英音女), loongeva_v3.6(美音女), loongjohn(美音男)'},
      {value:'cosyvoice-v3-plus',label:'CosyVoice v3 Plus (高清音质·音色最丰富)',group:'CosyVoice 系列',voices:'loongandy_v3(美式男), loongbeth_v3(美式女), loongemily_v3(英式女), loongeric_v3(英式男)'},
      {value:'cosyvoice-v3-flash',label:'CosyVoice v3 Flash (快速·性价比高·指令控制)',group:'CosyVoice 系列',voices:'loongandy_v3(美式男), loongbeth_v3(美式女), loongemily_v3(英式女), loongeric_v3(英式男)'},
    ]
    const groupedTtsModels = computed(() => {
      const groups = {}
      for (const m of TTS_MODELS) {
        if (!groups[m.group]) groups[m.group] = []
        groups[m.group].push(m)
      }
      return groups
    })

    // 单词库
    const wordsData = ref([])
    const wordsLoading = ref(false)
    const wordTotal = ref(0)
    const wordPage = ref(1)
    const wordPageSize = ref(20)
    const wordSearch = ref('')
    const selectedWords = ref([])
    const showAddWordDialog = ref(false)
    const addWordForm = reactive({word:'',source:''})
    const addWordLoading = ref(false)
    const showNoteDialog = ref(false)
    const noteWord = ref(null)
    const noteText = ref('')
    const noteLoading = ref(false)

    // 导入
    const importText = ref('')
    const parsing = ref(false)
    const parseResult = ref(null)
    const importing = ref(false)
    const fileInputRef = ref(null)

    // 编译
    const compileWords = ref([])
    const compileForm = reactive({
      content_form:'dialogue',scene_type:'meeting',difficulty:'intermediate',
      length_level:'medium',include_translation:true,generate_audio:true,
      tts_model:'qwen-audio-3.0-tts-plus',
    })
    const compiling = ref(false)

    // 历史
    const historyData = ref([])
    const historyTotal = ref(0)
    const historyPage = ref(1)
    const historyLoading = ref(false)
    const historyFilter = ref('')

    // 熟词生意
    const polysemySearch = ref('')
    const polysemyResult = ref(undefined)
    const polysemyHot = ref([])
    const polysemyTotal = ref(0)
    const polysemyPage = ref(1)
    const polysemyLoading = ref(false)

    // 结果
    const resultData = ref(null)
    const resultLoading = ref(false)
    const showZh = ref(true)
    const audioLoading = ref(false)
    const regenerateTtsModel = ref('qwen-audio-3.0-tts-plus')

    // ====== 工具函数 ======
    function formatDate(d) {
      if (!d) return '-'
      const dt = new Date(d)
      if (isNaN(dt)) return '-'
      return dt.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
    }
    function contentFormText(v) {
      const m = {dialogue:'商务对话',email:'邮件',memo:'备忘录',report:'报告摘要',short_narrative:'短叙述'}
      return m[v]||v
    }
    function sceneTypeText(v) {
      const m = {meeting:'会议',travel:'差旅',procurement:'采购',hr:'HR',finance:'财务',customer_service:'客户服务',project_management:'项目管理',marketing:'市场推广'}
      return m[v]||v
    }
    function difficultyText(v) {
      const m = {beginner:'初级',intermediate:'中级',advanced:'高级'}
      return m[v]||v
    }
    function getModelDefaultVoice(model) {
      const map = {
        'qwen-audio-3.0-tts-plus':'loongmary (温暖英音·女)',
        'cosyvoice-v3-plus':'loongandy_v3 (美式英文男)',
        'cosyvoice-v3-flash':'loongandy_v3 (美式英文男)',
      }
      return map[model] || 'loongandy_v3 (美式英文男)'
    }

    // ====== API 请求 ======
    async function api(url, opts={}) {
      const resp = await fetch(url, {
        headers:{'Content-Type':'application/json',...opts.headers},
        ...opts
      })
      if (!resp.ok) {
        const err = await resp.json().catch(()=>({detail:'请求失败'}))
        const detail = err.detail
        let msg = '请求失败'
        if (Array.isArray(detail)) {
          msg = detail.map(d => (d && d.msg) || JSON.stringify(d)).join('；')
        } else if (typeof detail === 'string') {
          msg = detail
        } else if (err && err.message) {
          msg = err.message
        } else if (detail && typeof detail === 'object') {
          msg = JSON.stringify(detail)
        }
        throw new Error(msg)
      }
      return resp.json()
    }

    // ====== 单词库 ======
    async function loadWords() {
      wordsLoading.value = true
      try {
        const res = await api(`/api/words?page=${wordPage.value}&page_size=${wordPageSize.value}&search=${encodeURIComponent(wordSearch.value)}`)
        wordsData.value = res.items
        wordTotal.value = res.total
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { wordsLoading.value = false }
    }
    async function updateWord(row, field, val) {
      try {
        await api(`/api/words/${row.id}`, {method:'PATCH', body:JSON.stringify({[field]:val})})
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
    }
    async function deleteWord(row) {
      try {
        await ElementPlus.ElMessageBox.confirm(`确定删除单词 "${row.word}" 吗？`,'提示',{confirmButtonText:'删除',cancelButtonText:'取消',type:'warning'})
        await api(`/api/words/${row.id}`, {method:'DELETE'})
        ElementPlus.ElMessage.success('删除成功')
        loadWords()
      } catch(e) { if (e!=='cancel') ElementPlus.ElMessage.error(e.message) }
    }
    function editNote(row) {
      noteWord.value = row
      noteText.value = row.note || ''
      showNoteDialog.value = true
    }
    async function saveNote() {
      if (!noteWord.value) return
      noteLoading.value = true
      try {
        await api(`/api/words/${noteWord.value.id}`, {method:'PATCH', body:JSON.stringify({note:noteText.value})})
        noteWord.value.note = noteText.value
        ElementPlus.ElMessage.success('备注已保存')
        showNoteDialog.value = false
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { noteLoading.value = false }
    }
    async function submitAddWord() {
      if (!addWordForm.word.trim()) { ElementPlus.ElMessage.warning('请输入单词'); return }
      addWordLoading.value = true
      try {
        await api('/api/words', {method:'POST', body:JSON.stringify({word:addWordForm.word.trim(),source:addWordForm.source})})
        ElementPlus.ElMessage.success('单词添加成功')
        showAddWordDialog.value = false
        addWordForm.word = ''
        addWordForm.source = ''
        loadWords()
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { addWordLoading.value = false }
    }
    function goCompile() {
      if (selectedWords.value.length===0) { ElementPlus.ElMessage.warning('请先选择单词'); return }
      compileWords.value = [...selectedWords.value]
      currentPage.value = 'compile'
    }

    // ====== 导入 ======
    async function handleParse() {
      if (!importText.value.trim()) { ElementPlus.ElMessage.warning('请输入单词'); return }
      parsing.value = true
      try {
        parseResult.value = await api('/api/words/parse', {method:'POST', body:JSON.stringify({text:importText.value})})
        if (parseResult.value.words.length===0) ElementPlus.ElMessage.warning('未解析到有效单词')
        else ElementPlus.ElMessage.success(`解析到 ${parseResult.value.words.length} 个有效单词`)
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { parsing.value = false }
    }
    async function doImport() {
      if (!parseResult.value || parseResult.value.words.length===0) return
      importing.value = true
      try {
        const res = await api('/api/words/import', {method:'POST', body:JSON.stringify({words:parseResult.value.words,source:'manual_import'})})
        ElementPlus.ElMessage.success(`导入完成：成功 ${res.imported} 个，重复 ${res.duplicated} 个`)
        currentPage.value = 'words'
        loadWords()
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { importing.value = false }
    }
    function triggerFileInput() { fileInputRef.value?.click() }
    function handleFileChange(e) {
      const file = e.target.files?.[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = (ev) => {
        importText.value = ev.target?.result || ''
        ElementPlus.ElMessage.success('文件已读取，点击"预览解析"查看结果')
      }
      reader.readAsText(file)
    }

    // ====== 编译 ======
    async function handleCompile() {
      if (compileWords.value.length===0) { ElementPlus.ElMessage.warning('请先选择单词'); return }
      compiling.value = true
      try {
        const words = compileWords.value.map(w=>w.word).join(', ')
        const res = await api('/api/generate', {method:'POST', body:JSON.stringify({
          words,
          content_form: compileForm.content_form,
          scene_type: compileForm.scene_type,
          difficulty: compileForm.difficulty,
          length_level: compileForm.length_level,
          include_translation: compileForm.include_translation,
          generate_audio_immediately: compileForm.generate_audio,
          tts_model: compileForm.tts_model,
        })})
        ElementPlus.ElMessage.success('编译完成')
        resultData.value = res
        regenerateTtsModel.value = res.tts_model || compileForm.tts_model
        currentPage.value = 'result'
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { compiling.value = false }
    }

    // ====== 历史 ======
    async function loadHistory() {
      historyLoading.value = true
      try {
        const textsRes = await api(`/api/texts?page=${historyPage.value}&content_form=${historyFilter.value||''}`)
        historyData.value = textsRes.items
        historyTotal.value = textsRes.total
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { historyLoading.value = false }
    }
    async function viewResult(h) {
      resultLoading.value = true
      currentPage.value = 'result'
      try {
        const res = await api(`/api/generations/${h.id}`)
        resultData.value = res
        regenerateTtsModel.value = res.tts_model || 'cosyvoice-v3-flash'
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { resultLoading.value = false }
    }

    // ====== 熟词生意 ======
    async function searchPolysemy() {
      if (!polysemySearch.value.trim()) { ElementPlus.ElMessage.warning('请输入单词'); return }
      try {
        polysemyResult.value = await api(`/api/polysemy?word=${encodeURIComponent(polysemySearch.value.trim())}`)
      } catch(e) {
        polysemyResult.value = null
      }
    }
    async function loadPolysemyHot() {
      polysemyLoading.value = true
      try {
        const res = await api(`/api/polysemy/hot?page=${polysemyPage.value}`)
        polysemyHot.value = res.items
        polysemyTotal.value = res.total
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
      finally { polysemyLoading.value = false }
    }

    // ====== 结果 ======
    async function toggleFavorite() {
      if (!resultData.value) return
      try {
        const newFav = !resultData.value.is_favorited
        await api(`/api/texts/${resultData.value.id}/favorite`, {method:'POST', body:JSON.stringify({favorited:newFav})})
        resultData.value.is_favorited = newFav
        ElementPlus.ElMessage.success(newFav?'已收藏':'已取消收藏')
      } catch(e) { ElementPlus.ElMessage.error(e.message) }
    }
    async function regenerateAudio() {
      if (!resultData.value) return
      audioLoading.value = true
      try {
        const res = await api(`/api/texts/${resultData.value.id}/regenerate-audio`, {method:'POST', body:JSON.stringify({tts_model: regenerateTtsModel.value})})
        resultData.value.audio_url = res.url
        resultData.value.audio_error = null
        resultData.value.tts_model = res.tts_model || regenerateTtsModel.value
        if (res.cached) {
          ElementPlus.ElMessage.info('音频已存在，直接复用')
        } else {
          ElementPlus.ElMessage.success('音频已生成')
        }
      } catch(e) {
        resultData.value.audio_error = e.message
        ElementPlus.ElMessage.error(e.message)
      } finally { audioLoading.value = false }
    }

    // ====== 菜单 ======
    function handleMenuSelect(idx) {
      currentPage.value = idx
      if (idx==='words') loadWords()
      if (idx==='history') loadHistory()
      if (idx==='polysemy') loadPolysemyHot()
    }

    // ====== 高亮 ======
    const highlightedResult = computed(() => {
      if (!resultData.value || !resultData.value.body_en) return ''
      let text = resultData.value.body_en.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      const allWords = [...(resultData.value.included_words||[]), ...(resultData.value.missing_words||[])]
      allWords.sort((a,b)=>b.length-a.length)
      for (const w of allWords) {
        const re = new RegExp('\\b('+w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'[a-z]*)\\b','gi')
        const isHit = (resultData.value.included_words||[]).includes(w)
        text = text.replace(re, isHit ? '<span class="word-hit">$1</span>' : '<span class="word-miss">$1</span>')
      }
      return text
    })

    // ====== 初始化 ======
    onMounted(() => {
      loadWords()
      loadPolysemyHot()
      // 加载每日用量
      api('/api/health').then(r => {
        if (r && r.daily_usage) {
          dailyUsage.value = {
            ai: r.daily_usage.ai,
            tts: r.daily_usage.tts,
            ai_limit: r.daily_usage.ai_limit,
            tts_limit: r.daily_usage.tts_limit,
          }
        }
      }).catch(()=>{})
    })

    // ====== 监听单词搜索 ======
    watch(wordSearch, () => {
      wordPage.value = 1
      loadWords()
    })

    watch(historyFilter, () => {
      historyPage.value = 1
      loadHistory()
    })

    // ====== 图标组件（必须返回，否则模板中 :icon="Search" 等绑定会报错） ======
    const Icons = {
      Search: ElIcons.Search,
      Upload: ElIcons.Upload,
      Plus: ElIcons.Plus,
      MagicStick: ElIcons.MagicStick,
      Back: ElIcons.Back,
      UploadFilled: ElIcons.UploadFilled,
      RefreshLeft: ElIcons.RefreshLeft,
      EditPen: ElIcons.EditPen,
      Delete: ElIcons.Delete,
      Refresh: ElIcons.Refresh,
      ArrowUp: ElIcons.ArrowUp,
      ArrowDown: ElIcons.ArrowDown,
      StarFilled: ElIcons.StarFilled,
      Star: ElIcons.Star,
      Headset: ElIcons.Headset,
    }

    return {
      currentPage, dailyUsage,
      CONTENT_FORMS, SCENE_TYPES, DIFFICULTY_LEVELS, LENGTH_LEVELS, TTS_MODELS, groupedTtsModels,
      wordsData, wordsLoading, wordTotal, wordPage, wordPageSize, wordSearch, selectedWords,
      showAddWordDialog, addWordForm, addWordLoading,
      showNoteDialog, noteWord, noteText, noteLoading,
      importText, parsing, parseResult, importing, fileInputRef,
      compileWords, compileForm, compiling,
      historyData, historyTotal, historyPage, historyLoading, historyFilter,
      polysemySearch, polysemyResult, polysemyHot, polysemyTotal, polysemyPage, polysemyLoading,
      resultData, resultLoading, showZh, audioLoading, regenerateTtsModel,
      formatDate, contentFormText, sceneTypeText, difficultyText, getModelDefaultVoice,
      loadWords, updateWord, deleteWord, editNote, saveNote, submitAddWord, goCompile,
      handleParse, doImport, triggerFileInput, handleFileChange,
      handleCompile, loadHistory, viewResult,
      searchPolysemy, loadPolysemyHot,
      toggleFavorite, regenerateAudio, handleMenuSelect,
      highlightedResult,
      // 图标组件
      ...Icons,
    }
  }
})

// 注册所有图标
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(ElementPlus)
app.mount('#app')
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)

# ========================================================================
# 启动
# ========================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
