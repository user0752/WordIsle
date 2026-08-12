"""
TOEIC MVP API 路由
===================
所有业务 API 路由，使用 FastAPI APIRouter。
"""

import asyncio
import json
import sqlite3
import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from config import *
from db import *
from services import *

router = APIRouter()


# ========================================================================
# 内部辅助函数
# ========================================================================

async def _safe_json(req: Request) -> dict:
    """读取请求体 JSON：空 body 返回 {}，非法 JSON 抛 400 而非 500。"""
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


def _to_bool(v) -> bool:
    """将 JSON 中可能为字符串的布尔值统一解析为 bool（避免 'false' 被当 True）。"""
    return v in (True, 1, "1", "true", "True", "yes")


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    """安全解析整数参数，非法值回退 default，越界夹取到 [lo, hi]。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def _coerce_str(v, fallback: str = "") -> str:
    """把任意请求值安全转成字符串（list 用空格拼接）。"""
    if isinstance(v, list):
        return " ".join(str(x) for x in v if str(x).strip())
    if isinstance(v, str):
        return v
    return str(v) if v is not None else fallback

def _delete_generation(gen_id: str, not_found_msg: str = "记录不存在") -> dict:
    """删除生成记录及其关联的音频和图片文件（先删库记录，成功后再清理文件）。"""
    conn = get_db()
    try:
        gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
        if not gen:
            raise HTTPException(404, not_found_msg)
        audio_files = [a["file_name"] for a in conn.execute("SELECT file_name FROM audios WHERE generation_id=?", (gen_id,)).fetchall()]
        image_names = [Path(p["image_url"]).name for p in json.loads(gen["panels"] or "[]") if p.get("image_url")]
        conn.execute("DELETE FROM generations WHERE id=?", (gen_id,))
        conn.commit()
    finally:
        conn.close()
    for fn in audio_files:
        (AUDIOS_DIR / fn).unlink(missing_ok=True)
    for name in image_names:
        target = IMAGES_DIR / name
        if target.is_relative_to(IMAGES_DIR):
            target.unlink(missing_ok=True)
    return {"ok": True}


async def _generate_audio(gen_id: str, voice: str, speed: float, tts_model: str, not_found_msg: str = "记录不存在") -> dict:
    """为指定生成记录合成 TTS 音频（含去重、配额检查、落盘）。"""
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    conn.close()
    if not gen:
        raise HTTPException(404, not_found_msg)
    if not gen["body_en"]:
        raise HTTPException(400, "文本无英文内容")

    # 去重：同一文本同一音色同一模型的音频已存在则直接返回
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM audios WHERE generation_id=? AND voice=? AND speed=? AND tts_model=?",
        (gen_id, voice, speed, tts_model),
    ).fetchone()
    conn.close()
    if existing:
        return {
            "id": existing["id"], "generation_id": gen_id,
            "file_name": existing["file_name"], "url": f"/audios/{existing['file_name']}",
            "cached": True, "tts_model": tts_model,
        }

    if not consume_daily_quota("tts"):
        raise HTTPException(429, f"今日 TTS 合成已达上限 ({DAILY_TTS_LIMIT} 次)")

    audio_bytes = await call_tts(gen["body_en"], voice, speed, tts_model)
    file_name = f"{gen_id}_{voice}_{int(speed*100)}.mp3"
    (AUDIOS_DIR / file_name).write_bytes(audio_bytes)

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
            (gen_id, file_name, voice, speed, tts_model),
        )
        if cur.rowcount == 0:
            existing = conn.execute(
                "SELECT * FROM audios WHERE generation_id=? AND voice=? AND speed=? AND tts_model=?",
                (gen_id, voice, speed, tts_model),
            ).fetchone()
            conn.commit()
            if existing:
                return {
                    "id": existing["id"], "generation_id": gen_id,
                    "file_name": existing["file_name"], "url": f"/audios/{existing['file_name']}",
                    "cached": True, "tts_model": tts_model,
                }
        audio_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    return {"id": audio_id, "generation_id": gen_id, "file_name": file_name,
            "url": f"/audios/{file_name}", "cached": False, "tts_model": tts_model}


# ========================================================================
# 生成 API
# ========================================================================

@router.post("/api/generate")
async def generate(req: Request):
    body = await _safe_json(req)
    raw_words    = _coerce_str(body.get("words", ""))
    panel_count  = _clamp_int(body.get("panel_count", 4), 3, 8, 4)
    theme_hint   = body.get("theme_hint", "") or ""
    image_model  = body.get("image_model", IMAGE_MODEL)
    generate_audio = _to_bool(body.get("generate_audio_immediately", False))
    tts_model    = body.get("tts_model", TTS_MODEL) if generate_audio else None
    style        = body.get("style", "absurd")  # 缺省 'absurd'；显式传 '' 为旧版微电影

    words = normalize_words(raw_words)
    if not words:
        raise HTTPException(400, "请至少输入一个有效单词")
    if len(words) > 30:
        raise HTTPException(400, f"单次最多 30 个单词，当前 {len(words)} 个")
    # 新风格固定 3 panel，跳过校验；旧风格校验 3/4/5
    if style not in ("absurd", "conflict") and panel_count not in (3, 4, 5):
        raise HTTPException(400, "画面数量只能是 3、4 或 5")

    if not consume_daily_quota("ai"):
        raise HTTPException(429, f"今日 AI 生成已达上限 ({DAILY_AI_LIMIT} 次)")

    gen_id = str(uuid.uuid4())[:8]
    result, usage = await call_deepseek(words, panel_count, theme_hint, style=style)

    # 防护：LLM 未返回任何画面时，直接报错而非写入空记录
    if not result.get("panels"):
        raise HTTPException(502, "AI 未能生成画面内容，请重试")

    # 实际 panel 数（新风格固定 3）
    actual_panel_count = len(result.get("panels", [])) or panel_count

    panels = result.get("panels", [])

    # 预扣图片配额（生成前检查，避免超限后仍返回图片）
    if len(panels) > 0 and not consume_daily_quota("image", len(panels)):
        for p in panels:
            p["image_url"] = None
            p["image_error"] = f"今日文生图已达上限 ({DAILY_IMAGE_LIMIT} 次)"
        image_results = []
    else:
        # 并发生成每个画面的图片
        image_tasks = [
            generate_panel_image(p.get("image_prompt", ""), image_model, gen_id, p.get("scene_index", idx + 1), style=style)
            for idx, p in enumerate(panels)
        ]
        if image_tasks:
            cfg = _get_image_model_config(image_model)
            if cfg.get("endpoint") == "multimodal":
                # 旗舰档：串行（RPM=2，且每次耗时较长）
                image_results = []
                for t in image_tasks:
                    image_results.append(await t)
            else:
                # 均衡/性价比档：并发
                image_results = await asyncio.gather(*image_tasks)
        else:
            image_results = []

        # 把图片 URL 写回 panels
        for p, ir in zip(panels, image_results):
            p["image_url"] = ir["url"]
            p["image_error"] = ir["error"]

    image_ok_count = sum(1 for ir in image_results if ir["url"])

    # 拼接完整英文正文（供 TTS 和历史预览用）
    full_body_en = " ".join(p.get("sentence_en", "") for p in panels)

    # 入库
    conn = get_db()
    conn.execute("""
        INSERT INTO generations (id,words,panel_count,theme_hint,
                                 story_title,theme,story_synopsis,body_en,model,image_model,panels,
                                 polysemy_notes,included_words,missing_words,ending_moral,
                                 generation_type,style)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        gen_id,
        json.dumps(words),
        actual_panel_count,
        theme_hint,
        result.get("story_title", ""),
        result.get("theme", ""),
        result.get("story_synopsis", ""),
        full_body_en,
        DEEPSEEK_MODEL,
        image_model,
        json.dumps(panels),
        json.dumps(result.get("polysemy_notes", {})),
        json.dumps(result.get("included_words", [])),
        json.dumps(result.get("missing_words", [])),
        result.get("ending_moral", ""),
        "batch",
        style,
    ))
    for w in words:
        conn.execute("INSERT OR IGNORE INTO words(word) VALUES(?)", (w,))
    conn.commit()
    conn.close()

    resp = {
        "id": gen_id,
        "status": "success",
        "generation_type": "batch",
        "style": style,
        "story_title": result.get("story_title", ""),
        "theme": result.get("theme", ""),
        "story_synopsis": result.get("story_synopsis", ""),
        "ending_moral": result.get("ending_moral", ""),
        "panels": panels,
        "words": words,
        "included_words": result.get("included_words", []),
        "missing_words": result.get("missing_words", []),
        "polysemy_notes": result.get("polysemy_notes", {}),
        "panel_count": actual_panel_count,
        "image_model": image_model,
        "image_success_count": image_ok_count,
        "has_audio": False,
        "audio_id": None,
    }

    # 可选：为整条剧情串联生成 TTS 音频
    if generate_audio and full_body_en:
        if not consume_daily_quota("tts"):
            resp["audio_error"] = f"今日 TTS 合成已达上限 ({DAILY_TTS_LIMIT} 次)，未生成音频"
        else:
            try:
                audio_bytes = await call_tts(full_body_en, TTS_VOICE, 1.0, tts_model)
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


@router.post("/api/generations/{gen_id}/audio")
async def generate_audio(gen_id: str, req: Request):
    body = await _safe_json(req)
    voice = body.get("voice", TTS_VOICE)
    speed = body.get("speed", 1.0)
    tts_model = body.get("tts_model", TTS_MODEL)
    voice, speed = validate_tts_params(voice, speed)
    return await _generate_audio(gen_id, voice, speed, tts_model, "生成记录不存在")


# ========================================================================
# 单点深耕 API
# ========================================================================

@router.post("/api/single/compile")
async def single_compile(req: Request):
    """单点深耕：给定 1 个单词 → 生成词伙 + 场景句 + 派生词 + 1 张记忆钩子图。"""
    body = await _safe_json(req)
    word_raw = (body.get("word") or "").strip().lower()
    theme_hint = body.get("theme_hint", "")
    image_model = body.get("image_model", IMAGE_MODEL)
    generate_audio_immediately = _to_bool(body.get("generate_audio_immediately", False))
    tts_model = body.get("tts_model", TTS_MODEL) if generate_audio_immediately else None

    # 单词清洗：只允许字母、连字符、撇号
    import re as _re
    word_clean = _re.sub(r"[^a-zA-Z\-']", "", word_raw).lower()
    if not word_clean or len(word_clean) < 2:
        raise HTTPException(400, "请输入一个有效英文单词")

    if not consume_daily_quota("ai"):
        raise HTTPException(429, f"今日 AI 生成已达上限 ({DAILY_AI_LIMIT} 次)")

    gen_id = str(uuid.uuid4())[:8]
    result, usage = await call_deepseek_single(word_clean, theme_hint)

    # 生成 1 张图（先检查配额）
    image_url = None
    image_error = None
    if not consume_daily_quota("image", 1):
        image_error = f"今日文生图已达上限 ({DAILY_IMAGE_LIMIT} 次)"
    else:
        ir = await generate_single_image(result.get("image_prompt", ""), image_model, gen_id)
        image_url = ir["url"]
        image_error = ir["error"]

    # scene_sentence.en 用于 TTS 和 body_en
    scene_sentence = result.get("scene_sentence", {}) or {}
    body_en = scene_sentence.get("en", "") or ""

    # 入库：generation_type='single'，复用 panels 字段存 JSON 整体（schemaless 扩展，PRD 6.2 方案A）
    panels_payload = [{
        "scene_index": 1,
        "collocation": result.get("collocation", {}),
        "scene_sentence": scene_sentence,
        "image_prompt": result.get("image_prompt", ""),
        "hook_type": result.get("hook_type", ""),
        "image_url": image_url,
        "image_error": image_error,
        "derivatives": result.get("derivatives", []),
    }]

    conn = get_db()
    conn.execute("""
        INSERT INTO generations (id,words,panel_count,theme_hint,
                                 story_title,theme,story_synopsis,body_en,model,image_model,panels,
                                 polysemy_notes,included_words,missing_words,ending_moral,
                                 generation_type,style)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        gen_id,
        json.dumps([word_clean]),
        1,
        theme_hint,
        f"{word_clean} · 单点深耕",
        "单点深耕",
        scene_sentence.get("zh", ""),
        body_en,
        DEEPSEEK_MODEL,
        image_model,
        json.dumps(panels_payload, ensure_ascii=False),
        json.dumps({}, ensure_ascii=False),
        json.dumps([word_clean], ensure_ascii=False),
        json.dumps([], ensure_ascii=False),
        "",
        "single",
        "",
    ))
    # 若词库无该词，自动加入（与批量编译一致）
    conn.execute("INSERT OR IGNORE INTO words(word) VALUES(?)", (word_clean,))
    conn.commit()
    conn.close()

    resp = {
        "id": gen_id,
        "generation_type": "single",
        "status": "success",
        "word": word_clean,
        "collocation": result.get("collocation", {}),
        "scene_sentence": scene_sentence,
        "image_prompt": result.get("image_prompt", ""),
        "hook_type": result.get("hook_type", ""),
        "image_url": image_url,
        "image_error": image_error,
        "derivatives": result.get("derivatives", []),
        "image_model": image_model,
        "has_audio": False,
        "audio_id": None,
    }

    # 可选：即时合成场景句朗读音频
    if generate_audio_immediately and body_en:
        if not consume_daily_quota("tts"):
            resp["audio_error"] = f"今日 TTS 合成已达上限 ({DAILY_TTS_LIMIT} 次)，未生成音频"
        else:
            try:
                audio_bytes = await call_tts(body_en, TTS_VOICE, 1.0, tts_model)
                file_name = f"{gen_id}_{TTS_VOICE}_100.mp3"
                (AUDIOS_DIR / file_name).write_bytes(audio_bytes)
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


@router.post("/api/single/{gen_id}/audio")
async def single_generate_audio(gen_id: str, req: Request):
    """为单点深耕场景句生成朗读音频（后置生成）。"""
    body = await _safe_json(req)
    voice = body.get("voice", TTS_VOICE)
    speed = body.get("speed", 1.0)
    tts_model = body.get("tts_model", TTS_MODEL)
    voice, speed = validate_tts_params(voice, speed)
    return await _generate_audio(gen_id, voice, speed, tts_model, "单点深耕记录不存在")


@router.get("/api/generations")
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
        "generation_type": r["generation_type"] or "batch",
        "style": r["style"] or "",
        "story_title": r["story_title"],
        "theme": r["theme"],
        "story_synopsis": r["story_synopsis"],
        "words": json.loads(r["words"] or "[]"),
        "panel_count": r["panel_count"],
        "image_model": r["image_model"],
        "created_at": r["created_at"],
        "body_en": (r["body_en"][:100] + "...") if r["body_en"] and len(r["body_en"]) > 100 else (r["body_en"] or ""),
        "has_audio": bool(r["audio_file"]),
        "is_favorited": bool(r["is_favorited"]),
        "included_words": json.loads(r["included_words"] or "[]"),
        "missing_words": json.loads(r["missing_words"] or "[]"),
        "polysemy_notes": json.loads(r["polysemy_notes"] or "{}"),
        "first_image_url": (json.loads(r["panels"] or "[]")[0:1] or [{}])[0].get("image_url") if r["panels"] else None,
    } for r in rows]


@router.get("/api/generations/{gen_id}")
async def get_generation(gen_id: str):
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    aud = conn.execute("SELECT * FROM audios WHERE generation_id=? LIMIT 1", (gen_id,)).fetchone()
    conn.close()
    if not gen:
        raise HTTPException(404, "记录不存在")
    return {
        "id": gen["id"],
        "generation_type": gen["generation_type"] or "batch",
        "style": gen["style"] or "",
        "story_title": gen["story_title"],
        "theme": gen["theme"],
        "story_synopsis": gen["story_synopsis"],
        "ending_moral": gen["ending_moral"],
        "panels": json.loads(gen["panels"] or "[]"),
        "body_en": gen["body_en"],
        "words": json.loads(gen["words"] or "[]"),
        "panel_count": gen["panel_count"],
        "image_model": gen["image_model"],
        "included_words": json.loads(gen["included_words"] or "[]"),
        "missing_words": json.loads(gen["missing_words"] or "[]"),
        "polysemy_notes": json.loads(gen["polysemy_notes"] or "{}"),
        "is_favorited": bool(gen["is_favorited"]),
        "created_at": gen["created_at"],
        "audio_url": f"/audios/{aud['file_name']}" if aud else None,
        "audio_id": aud["id"] if aud else None,
        "has_audio": bool(aud),
        "tts_model": aud["tts_model"] if aud else None,
    }


@router.delete("/api/generations/{gen_id}")
async def delete_generation(gen_id: str):
    return _delete_generation(gen_id, "记录不存在")


# ========================================================================
# 健康检查
# ========================================================================

@router.get("/api/health")
async def health():
    today = date.today().isoformat()
    conn = get_db()
    row = conn.execute("SELECT * FROM daily_usage WHERE day=?", (today,)).fetchone()
    conn.close()
    usage = {"ai": 0, "tts": 0, "image": 0}
    if row:
        usage = {"ai": row["ai_count"], "tts": row["tts_count"], "image": row["image_count"]}
    return {
        "status": "ok",
        "db": DB_PATH.exists(),
        "deepseek_key": bool(DEEPSEEK_API_KEY),
        "tts_key": bool(TTS_API_KEY),
        "image_key": bool(IMAGE_API_KEY or TOKENRHYTHM_API_KEY),
        "daily_usage": {**usage, "ai_limit": DAILY_AI_LIMIT, "tts_limit": DAILY_TTS_LIMIT, "image_limit": DAILY_IMAGE_LIMIT},
    }


# ========================================================================
# 文生图模型
# ========================================================================

@router.get("/api/image-models")
async def list_image_models():
    """返回文生图模型三档列表，供前端下拉选择。"""
    return {"models": IMAGE_MODELS}


# ========================================================================
# 音频 API
# ========================================================================

@router.get("/api/audios/{audio_id}")
async def get_audio(audio_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM audios WHERE id=?", (audio_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "音频不存在")
    return dict(row)


@router.get("/api/audios/{audio_id}/stream")
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

@router.get("/api/words")
async def list_words(page: int = 1, page_size: int = 20, search: str = ""):
    page = max(1, page)
    page_size = _clamp_int(page_size, 1, 100, 20)
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


@router.post("/api/words")
async def create_word(req: Request):
    body = await _safe_json(req)
    word = body.get("word", "").strip().lower()
    if not word or len(word) < 2:
        raise HTTPException(400, "无效单词")
    pos = _coerce_str(body.get("pos", ""))
    meaning_zh = _coerce_str(body.get("meaning_zh", ""))
    conn = get_db()
    try:
        # 如果用户没填词性或释义，用 LLM 自动补充
        if not pos or not meaning_zh:
            enrich = await call_word_enrichment([word])
            if not enrich.get("skipped") and enrich.get("results"):
                for r in enrich["results"]:
                    if r["word"] == word:
                        if not pos:
                            pos = r["pos"]
                        if not meaning_zh:
                            meaning_zh = r["meaning_zh"]
                        break
        cur = conn.execute("INSERT INTO words (word, pos, meaning_zh) VALUES (?,?,?)", (word, pos, meaning_zh))
        wid = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"单词 '{word}' 已存在")
    finally:
        conn.close()


@router.patch("/api/words/{word_id}")
async def update_word(word_id: int, req: Request):
    body = await _safe_json(req)
    allowed = {"pos", "meaning_zh"}
    updates = {k: v for k, v in body.items() if k in allowed and isinstance(v, str)}
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


@router.delete("/api/words/{word_id}")
async def delete_word(word_id: int):
    conn = get_db()
    conn.execute("DELETE FROM words WHERE id=?", (word_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/api/words/batch-delete")
async def batch_delete_words(req: Request):
    """批量删除单词（word_scenes 等外键关联由 ON DELETE CASCADE 联动删除）。"""
    body = await _safe_json(req)
    ids = body.get("ids", []) or []
    cleaned = sorted({int(i) for i in ids if str(i).isdigit()})
    if not cleaned:
        raise HTTPException(400, "请提供要删除的单词 id 列表")
    if len(cleaned) > 500:
        raise HTTPException(400, "单次最多删除 500 个单词")
    placeholders = ",".join("?" * len(cleaned))
    conn = get_db()
    try:
        cur = conn.execute(f"DELETE FROM words WHERE id IN ({placeholders})", cleaned)
        deleted = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted, "count": len(cleaned)}


@router.post("/api/words/parse")
async def parse_words(req: Request):
    body = await _safe_json(req)
    text = _coerce_str(body.get("text", ""))
    words = normalize_words(text)
    conn = get_db()
    existing = set(r["word"] for r in conn.execute("SELECT word FROM words").fetchall())
    # 查找熟词僻意（在关闭连接前完成所有查询）
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


@router.post("/api/words/import")
async def import_words(req: Request):
    body = await _safe_json(req)
    word_list = body.get("words", [])
    if not isinstance(word_list, list):
        word_list = []
    conn = get_db()
    imported = 0
    duplicated = 0
    new_words = []
    enriched = 0
    try:
        for w in word_list:
            w = str(w).strip().lower()
            if not w or len(w) < 2:
                continue
            try:
                conn.execute("INSERT INTO words (word) VALUES (?)", (w,))
                imported += 1
                new_words.append(w)
            except sqlite3.IntegrityError:
                duplicated += 1
        conn.commit()

        # 入库与 LLM 补全解耦：已插入的词先提交，补全失败不影响导入结果
        if new_words:
            batch_size = 20
            for i in range(0, len(new_words), batch_size):
                batch = new_words[i:i + batch_size]
                try:
                    enrich = await call_word_enrichment(batch)
                except Exception:
                    continue
                if not enrich.get("skipped") and enrich.get("results"):
                    for r in enrich["results"]:
                        if r.get("pos") or r.get("meaning_zh"):
                            conn.execute(
                                "UPDATE words SET pos=?, meaning_zh=? WHERE word=?",
                                (r.get("pos", ""), r.get("meaning_zh", ""), r.get("word", "")),
                            )
                            enriched += 1
            conn.commit()
    finally:
        conn.close()
    return {"imported": imported, "duplicated": duplicated, "total_input": len(word_list), "enriched": enriched}


# ========================================================================
# 生成文本管理 API
# ========================================================================

@router.get("/api/texts")
async def list_texts(page: int = 1, search: str = "", favorited: int = 0, has_audio: int = 0,
                     generation_type: str = "", style: str = ""):
    page = max(1, page)
    conn = get_db()
    offset = (page - 1) * 20
    where, params = [], []
    if search:
        where.append("(story_title LIKE ? OR story_synopsis LIKE ? OR body_en LIKE ? OR words LIKE ?)")
        params += [f"%{search}%"] * 4
    if favorited:
        where.append("is_favorited = 1")
    if has_audio:
        where.append("id IN (SELECT generation_id FROM audios GROUP BY generation_id)")
    if generation_type:
        where.append("generation_type = ?")
        params.append(generation_type)
    if style:
        where.append("style = ?")
        params.append(style)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM generations{where_sql} ORDER BY created_at DESC LIMIT 20 OFFSET ?",
        params + [offset],
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM generations{where_sql}", params).fetchone()[0]
    # 批量查询当前页记录的音频状态
    audio_map = {}
    gen_ids = [r["id"] for r in rows]
    if gen_ids:
        qmarks = ",".join("?" * len(gen_ids))
        for a in conn.execute(
            f"SELECT generation_id, file_name FROM audios WHERE generation_id IN ({qmarks}) GROUP BY generation_id",
            gen_ids,
        ).fetchall():
            audio_map[a["generation_id"]] = a["file_name"]
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        panels = json.loads(d.get("panels", "[]"))
        d["panels"] = panels
        d["polysemy_notes"] = json.loads(d.get("polysemy_notes", "{}"))
        d["words"] = json.loads(d.get("words", "[]"))
        d["included_words"] = json.loads(d.get("included_words", "[]"))
        d["missing_words"] = json.loads(d.get("missing_words", "[]"))
        d["first_image_url"] = panels[0].get("image_url") if panels else None
        d["has_audio"] = bool(audio_map.get(r["id"]))
        d["audio_url"] = f"/audios/{audio_map[r['id']]}" if audio_map.get(r["id"]) else None
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": 20}


@router.get("/api/texts/recent")
async def list_recent_texts(limit: int = 5):
    """首页"最近生成"列表：取最近 N 条记录（带封面图）。"""
    limit = max(1, min(int(limit), 20))
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM generations ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        panels = json.loads(r["panels"] or "[]")
        items.append({
            "id": r["id"],
            "story_title": r["story_title"] or "",
            "theme": r["theme"] or "",
            "story_synopsis": r["story_synopsis"] or "",
            "generation_type": r["generation_type"] or "batch",
            "style": r["style"] or "",
            "panel_count": r["panel_count"],
            "words": json.loads(r["words"] or "[]"),
            "included_words": json.loads(r["included_words"] or "[]"),
            "first_image_url": panels[0].get("image_url") if panels else None,
            "created_at": r["created_at"],
        })
    return {"items": items}


@router.get("/api/texts/{text_id}")
async def get_text(text_id: str):
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (text_id,)).fetchone()
    conn.close()
    if not gen:
        raise HTTPException(404, "文本不存在")
    d = dict(gen)
    d["words"] = json.loads(d.get("words", "[]"))
    d["panels"] = json.loads(d.get("panels", "[]"))
    d["included_words"] = json.loads(d.get("included_words", "[]"))
    d["missing_words"] = json.loads(d.get("missing_words", "[]"))
    d["polysemy_notes"] = json.loads(d.get("polysemy_notes", "{}"))
    return d


@router.post("/api/texts/{text_id}/favorite")
async def favorite_text(text_id: str, req: Request):
    body = await _safe_json(req)
    favorited = _to_bool(body.get("favorited", False))
    conn = get_db()
    conn.execute("UPDATE generations SET is_favorited=? WHERE id=?", (1 if favorited else 0, text_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.delete("/api/texts/{text_id}")
async def delete_text(text_id: str):
    return _delete_generation(text_id, "文本不存在")


@router.post("/api/texts/{text_id}/regenerate-audio")
async def regenerate_audio_for_text(text_id: str, req: Request):
    body = await _safe_json(req)
    voice = body.get("voice", TTS_VOICE)
    speed = body.get("speed", 1.0)
    tts_model = body.get("tts_model", TTS_MODEL)
    voice, speed = validate_tts_params(voice, speed)
    return await _generate_audio(text_id, voice, speed, tts_model, "文本不存在")


# ========================================================================
# 熟词僻意 API
# ========================================================================

@router.get("/api/polysemy")
async def get_polysemy(word: str = ""):
    if not word:
        raise HTTPException(400, "请提供单词")
    conn = get_db()
    row = conn.execute("SELECT * FROM polysemy WHERE word=?", (word.strip().lower(),)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "未收录该词的熟词僻意")
    d = dict(row)
    d["collocations"] = json.loads(d.get("collocations", "[]"))
    return d


@router.get("/api/polysemy/hot")
async def polysemy_hot(page: int = 1):
    page = max(1, page)
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


@router.delete("/api/polysemy/{word}")
async def delete_polysemy(word: str):
    """从熟词僻意表删除指定单词。"""
    w = word.strip().lower()
    if not w:
        raise HTTPException(400, "请提供单词")
    conn = get_db()
    cur = conn.execute("DELETE FROM polysemy WHERE word=?", (w,))
    deleted = cur.rowcount or 0
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": deleted}


@router.post("/api/polysemy/batch-delete")
async def polysemy_batch_delete(req: Request):
    """批量删除熟词僻意词条。"""
    body = await _safe_json(req)
    words = body.get("words", []) or []
    cleaned = sorted({w.strip().lower() for w in words if isinstance(w, str) and w.strip()})
    if not cleaned:
        raise HTTPException(400, "请提供要删除的单词列表")
    if len(cleaned) > 500:
        raise HTTPException(400, "单次最多删除 500 个单词")
    placeholders = ",".join("?" * len(cleaned))
    conn = get_db()
    try:
        cur = conn.execute(f"DELETE FROM polysemy WHERE word IN ({placeholders})", cleaned)
        deleted = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted, "count": len(cleaned)}


@router.get("/api/polysemy/candidates")
async def polysemy_candidates(limit: int = 100):
    """获取单词库中尚未收录到熟词僻意表的候选词（仅查询，不调用LLM）。"""
    limit = _clamp_int(limit, 1, 500, 100)
    conn = get_db()
    rows = conn.execute(
        """SELECT w.id, w.word, w.pos, w.meaning_zh, w.created_at
           FROM words w
           LEFT JOIN polysemy p ON p.word = w.word
           WHERE p.word IS NULL
           ORDER BY w.created_at DESC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    total = conn.execute(
        """SELECT COUNT(*) FROM words w
           LEFT JOIN polysemy p ON p.word = w.word
           WHERE p.word IS NULL"""
    ).fetchone()[0]
    conn.close()
    return {"items": [dict(r) for r in rows], "total": total, "limit": limit}


@router.post("/api/polysemy/auto-detect")
async def polysemy_auto_detect(req: Request):
    """自动检测单词库中的候选词，调用 LLM 判断是否为托业高频熟词僻意，是则自动入库。

    请求体（可选）:
      - batch_size: 一次送给 LLM 的单词数，默认 20，建议 10~30
      - max_batches: 最多处理几批，默认 5（即一次最多处理 100 词，防止超配额）
    """
    body = await _safe_json(req)
    batch_size = _clamp_int(body.get("batch_size", 20), 5, 50, 20)
    max_batches = _clamp_int(body.get("max_batches", 5), 1, 20, 5)

    # 1) 获取候选词（总量够多一些，后续分批）
    conn = get_db()
    candidates = [
        r["word"] for r in conn.execute(
            """SELECT w.word FROM words w
               LEFT JOIN polysemy p ON p.word = w.word
               WHERE p.word IS NULL
               ORDER BY w.created_at DESC
               LIMIT ?""",
            (batch_size * max_batches,),
        ).fetchall()
    ]
    conn.close()
    if not candidates:
        return {
            "ok": True,
            "skipped_reason": "no_candidates",
            "message": "单词库中所有单词均已在熟词僻意表中，没有新的候选词。",
            "candidate_count": 0,
            "added_count": 0,
            "rejected_count": 0,
            "added_words": [],
            "rejected_words": [],
        }

    # 2) 分批调用 LLM 检测并入库
    added_words = []
    rejected_words = []
    total_ai_cost = 0
    batches_processed = 0

    for i in range(0, len(candidates), batch_size):
        if batches_processed >= max_batches:
            break
        batch = candidates[i:i + batch_size]
        if not batch:
            break

        # 配额检查（每批占 1 次 AI 配额）
        if not consume_daily_quota("ai"):
            return {
                "ok": False,
                "skipped_reason": "ai_quota_exceeded",
                "message": f"今日 AI 生成已达上限 ({DAILY_AI_LIMIT} 次)，请明天再试。",
                "candidate_count": len(candidates),
                "added_count": len(added_words),
                "rejected_count": len(rejected_words),
                "added_words": added_words,
                "rejected_words": rejected_words,
            }
        total_ai_cost += 1

        try:
            detect_res = await call_polysemy_detection(batch)
        except HTTPException as e:
            # 配额错误或服务错误：归还已扣配额？这里简化，直接中止并返回已处理结果
            return {
                "ok": False,
                "skipped_reason": "llm_error",
                "message": f"LLM 调用失败: {e.detail}",
                "candidate_count": len(candidates),
                "added_count": len(added_words),
                "rejected_count": len(rejected_words),
                "added_words": added_words,
                "rejected_words": rejected_words,
            }
        except Exception as e:
            return {
                "ok": False,
                "skipped_reason": "llm_error",
                "message": f"LLM 调用异常: {e}",
                "candidate_count": len(candidates),
                "added_count": len(added_words),
                "rejected_count": len(rejected_words),
                "added_words": added_words,
                "rejected_words": rejected_words,
            }

        results = detect_res.get("results", [])
        # 3) 写入 polysemy 表
        conn = get_db()
        try:
            for item in results:
                w = item.get("word", "").strip().lower()
                if not w or w not in batch:
                    continue
                if item.get("is_polysemy") is True:
                    # 字段兜底
                    col = json.dumps(item.get("collocations") or [], ensure_ascii=False)
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO polysemy
                               (word, common_meaning_zh, common_meaning_en,
                                business_meaning_zh, business_meaning_en,
                                example_en, example_zh, collocations,
                                toc_part, frequency_level)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (
                                w,
                                item.get("common_meaning_zh", ""),
                                item.get("common_meaning_en", ""),
                                item.get("business_meaning_zh", ""),
                                item.get("business_meaning_en", ""),
                                item.get("example_en", ""),
                                item.get("example_zh", ""),
                                col,
                                item.get("toc_part", ""),
                                item.get("frequency_level", ""),
                            ),
                        )
                        added_words.append(w)
                    except Exception:
                        # 单词重复或字段异常：跳过
                        rejected_words.append(w)
                else:
                    rejected_words.append(w)
            conn.commit()
        finally:
            conn.close()
        batches_processed += 1

    return {
        "ok": True,
        "skipped_reason": None,
        "message": (
            f"完成！共检测候选词 {len(candidates)} 个（{batches_processed} 批 / AI 配额消耗 {total_ai_cost} 次），"
            f"新增熟词僻意 {len(added_words)} 个，判定不匹配 {len(rejected_words)} 个。"
        ),
        "candidate_count": len(candidates),
        "batches_processed": batches_processed,
        "ai_quota_used": total_ai_cost,
        "added_count": len(added_words),
        "rejected_count": len(rejected_words),
        "added_words": added_words,
        "rejected_words": rejected_words,
    }


# ========================================================================
# 场景聚汇 API
# ========================================================================

def _row_to_scene(row: sqlite3.Row) -> dict:
    """把 scenes 行转换为 dict。"""
    return {
        "id": row["id"],
        "name_en": row["name_en"],
        "name_zh": row["name_zh"],
        "description": row["description"] or "",
        "cover_image_url": row["cover_image_url"] or "",
        "status": row["status"] or "active",
        "created_at": row["created_at"],
    }


@router.get("/api/scenes")
def list_scenes():
    """获取所有场景概览（含词数统计）。"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT s.*, COUNT(ws.word_id) AS word_count,
                   (SELECT COUNT(*) FROM scene_collocations sc WHERE sc.scene_id = s.id) AS collocations_count
            FROM scenes s
            LEFT JOIN word_scenes ws ON ws.scene_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at
        """).fetchall()
        result = []
        for r in rows:
            scene = _row_to_scene(r)
            scene["word_count"] = r["word_count"]
            scene["collocations_count"] = r["collocations_count"]
            result.append(scene)
        return {"scenes": result, "total": len(result)}
    finally:
        conn.close()


@router.get("/api/scenes/suggestions")
def list_scene_suggestions():
    """获取待采纳的新场景建议（暂存于内存，每次 detect 返回时由前端管理）。
    本接口返回空列表占位，建议总是由前端从最近的 detect 结果取。"""
    return {"suggestions": []}


@router.get("/api/scenes/{scene_id}")
def get_scene(scene_id: int):
    """获取单个场景详情，含词列表与词伙搭配。"""
    conn = get_db()
    try:
        s = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        if not s:
            raise HTTPException(404, "场景不存在")
        scene = _row_to_scene(s)

        words = conn.execute("""
            SELECT w.id, w.word, w.pos, w.meaning_zh, ws.created_at AS assigned_at
            FROM word_scenes ws
            JOIN words w ON w.id = ws.word_id
            WHERE ws.scene_id = ?
            ORDER BY ws.created_at DESC
        """, (scene_id,)).fetchall()
        scene["words"] = [dict(w) for w in words]

        cols = conn.execute("""
            SELECT * FROM scene_collocations WHERE scene_id = ? ORDER BY created_at DESC
        """, (scene_id,)).fetchall()
        scene["collocations"] = []
        for c in cols:
            d = dict(c)
            d["words"] = json.loads(d.get("words") or "[]")
            scene["collocations"].append(d)
        return scene
    finally:
        conn.close()


@router.post("/api/scenes/detect")
async def detect_scenes(request: Request):
    """场景自动检测：增量扫描未分类单词 + LLM 分类 + 新场景建议。
    Body 可选 {limit: int, force: bool}。"""
    body = await _safe_json(request)
    limit = _clamp_int(body.get("limit", 50), 1, 500, 50)
    force = _to_bool(body.get("force", False))

    conn = get_db()
    try:
        # 1) 已有场景
        existing = conn.execute(
            "SELECT id, name_en, name_zh, description FROM scenes WHERE status = 'active'"
        ).fetchall()
        existing_scenes = [dict(r) for r in existing]

        # 2) 待分类单词：未在 word_scenes 表中的词
        if force:
            unassigned = conn.execute("""
                SELECT w.id, w.word FROM words w WHERE w.word IS NOT NULL AND w.word != ''
                ORDER BY w.id LIMIT ?
            """, (limit,)).fetchall()
        else:
            unassigned = conn.execute("""
                SELECT w.id, w.word FROM words w
                LEFT JOIN word_scenes ws ON ws.word_id = w.id
                WHERE ws.word_id IS NULL AND w.word IS NOT NULL AND w.word != ''
                ORDER BY w.id LIMIT ?
            """, (limit,)).fetchall()

        words_to_assign = [r["word"] for r in unassigned]
        word_id_map = {r["word"].lower(): r["id"] for r in unassigned}

        if not words_to_assign:
            return {
                "scanned": 0,
                "assigned_count": 0,
                "low_confidence_count": 0,
                "new_scenes_suggested": [],
                "message": "没有待分类的单词",
            }

        # 3) 调用 LLM（消耗 AI 配额）
        if not consume_daily_quota("ai"):
            raise HTTPException(429, f"今日 AI 生成已达上限 ({DAILY_AI_LIMIT} 次)")
        result = await call_deepseek_scene_detect(words_to_assign, existing_scenes)
        assignments = result["scene_assignments"]
        new_scenes = result["new_scenes_suggested"]
        warning = result.get("warning", "")

        # 4) 写入 word_scenes（只清掉该词此前由 detect 自动归类的旧关系，保留 adopt/manual 关系）
        assigned = 0
        low_conf_count = 0
        involved_scene_ids = set()
        for a in assignments:
            wid = word_id_map.get(a["word"].lower())
            if wid is None:
                continue
            conn.execute("DELETE FROM word_scenes WHERE word_id = ? AND source = 'detect'", (wid,))
            conn.execute(
                "INSERT OR IGNORE INTO word_scenes (word_id, scene_id, source) VALUES (?,?,?)",
                (wid, a["scene_id"], "detect"),
            )
            involved_scene_ids.add(a["scene_id"])
            assigned += 1
            if a.get("low_confidence", False):
                low_conf_count += 1
        conn.commit()

        # 5) 为本次涉及的场景生成/刷新词伙搭配（写入 scene_collocations，PRD 4.2 第 5 步）
        collocations_generated = 0
        for sid in involved_scene_ids:
            if not consume_daily_quota("ai"):
                break
            scene_row = conn.execute("SELECT name_en, name_zh FROM scenes WHERE id = ?", (sid,)).fetchone()
            if not scene_row:
                continue
            scene_words = [r["word"] for r in conn.execute(
                "SELECT w.word FROM word_scenes ws JOIN words w ON w.id = ws.word_id WHERE ws.scene_id = ?",
                (sid,),
            ).fetchall()]
            try:
                cols = await call_deepseek_scene_collocations(scene_words, scene_row["name_en"], scene_row["name_zh"] or "")
            except Exception:
                continue
            if cols:
                conn.execute("DELETE FROM scene_collocations WHERE scene_id = ?", (sid,))
                for c in cols:
                    conn.execute(
                        "INSERT INTO scene_collocations (scene_id, phrase_en, phrase_zh, words, example_en, example_zh) VALUES (?,?,?,?,?,?)",
                        (sid, c["phrase_en"], c["phrase_zh"], json.dumps(c["words"], ensure_ascii=False),
                         c["example_en"], c["example_zh"]),
                    )
                collocations_generated += len(cols)
        if collocations_generated:
            conn.commit()

        msg = f"扫描 {len(words_to_assign)} 词，已归类 {assigned} 词，低置信度 {low_conf_count} 词，建议新场景 {len(new_scenes)} 个"
        if warning:
            msg = f"⚠️ {warning}"
        return {
            "scanned": len(words_to_assign),
            "assigned_count": assigned,
            "low_confidence_count": low_conf_count,
            "new_scenes_suggested": new_scenes,
            "warning": warning,
            "collocations_generated": collocations_generated,
            "message": msg,
        }
    finally:
        conn.close()


@router.post("/api/scenes/adopt")
async def adopt_scene(request: Request):
    """采纳一个新场景建议并立即把对应词归到该场景。
    Body: {name, name_zh, description, suggested_words: [...]}"""
    body = await _safe_json(request)
    name_en = str(body.get("name") or body.get("name_en") or "").strip()
    name_zh = str(body.get("name_zh") or "").strip()
    desc = str(body.get("description") or "").strip()
    suggested_words = body.get("suggested_words") or []
    if not name_en:
        raise HTTPException(400, "name 必填")

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO scenes (name_en, name_zh, description, status) VALUES (?,?,?,?)",
            (name_en, name_zh, desc, "active"),
        )
        new_scene_id = cur.lastrowid
        conn.commit()

        # 把 suggested_words 归到新场景
        assigned = 0
        if suggested_words:
            for w in suggested_words:
                wid_row = conn.execute("SELECT id FROM words WHERE LOWER(word) = LOWER(?)", (str(w),)).fetchone()
                if wid_row:
                    conn.execute(
                        "INSERT OR IGNORE INTO word_scenes (word_id, scene_id, source) VALUES (?,?,?)",
                        (wid_row["id"], new_scene_id, "adopt"),
                    )
                    assigned += 1
            conn.commit()

        # 为采纳的新场景生成词伙搭配（失败不阻塞采纳，AI 配额不足时跳过）
        collocations_generated = 0
        if consume_daily_quota("ai"):
            scene_words = [r["word"] for r in conn.execute(
                "SELECT w.word FROM word_scenes ws JOIN words w ON w.id = ws.word_id WHERE ws.scene_id = ?",
                (new_scene_id,),
            ).fetchall()]
            try:
                cols = await call_deepseek_scene_collocations(scene_words, name_en, name_zh)
            except Exception:
                cols = []
            if cols:
                for c in cols:
                    conn.execute(
                        "INSERT INTO scene_collocations (scene_id, phrase_en, phrase_zh, words, example_en, example_zh) VALUES (?,?,?,?,?,?)",
                        (new_scene_id, c["phrase_en"], c["phrase_zh"], json.dumps(c["words"], ensure_ascii=False),
                         c["example_en"], c["example_zh"]),
                    )
                collocations_generated = len(cols)
                conn.commit()

        return {
            "scene_id": new_scene_id,
            "name_en": name_en,
            "name_zh": name_zh,
            "assigned_count": assigned,
            "collocations_generated": collocations_generated,
            "message": f"新场景「{name_en}」已创建，归并 {assigned} 个词",
        }
    finally:
        conn.close()


@router.patch("/api/scenes/{scene_id}")
async def update_scene(scene_id: int, request: Request):
    """编辑场景：可改 name/name_zh/description，增删词。
    Body: {name_en?, name_zh?, description?, add_words?: [word_id|str], remove_words?: [word_id|str]}"""
    body = await _safe_json(request)
    conn = get_db()
    try:
        s = conn.execute("SELECT id FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        if not s:
            raise HTTPException(404, "场景不存在")

        if "name_en" in body:
            new_name = str(body["name_en"]).strip()
            if not new_name:
                raise HTTPException(400, "场景英文名不能为空")
            conn.execute("UPDATE scenes SET name_en = ? WHERE id = ?", (new_name, scene_id))
        if "name_zh" in body:
            conn.execute("UPDATE scenes SET name_zh = ? WHERE id = ?", (str(body["name_zh"]).strip(), scene_id))
        if "description" in body:
            conn.execute("UPDATE scenes SET description = ? WHERE id = ?", (str(body["description"]).strip(), scene_id))

        added = 0
        for w in (body.get("add_words") or []):
            wid = _resolve_word_id(conn, w)
            if wid:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO word_scenes (word_id, scene_id, source) VALUES (?,?,?)",
                    (wid, scene_id, "manual"),
                )
                added += cur.rowcount or 0

        removed = 0
        for w in (body.get("remove_words") or []):
            wid = _resolve_word_id(conn, w)
            if wid:
                cur = conn.execute("DELETE FROM word_scenes WHERE word_id = ? AND scene_id = ?", (wid, scene_id))
                removed += cur.rowcount

        conn.commit()
        return {
            "scene_id": scene_id,
            "added_count": added,
            "removed_count": removed,
            "message": f"已更新场景（增 {added} / 删 {removed}）",
        }
    finally:
        conn.close()


def _resolve_word_id(conn: sqlite3.Connection, w) -> int | None:
    """把 word_id(int) 或 word(str) 解析成 id。"""
    if isinstance(w, int):
        return w
    s = str(w).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    row = conn.execute("SELECT id FROM words WHERE LOWER(word) = LOWER(?)", (s,)).fetchone()
    return row["id"] if row else None


@router.delete("/api/scenes/{scene_id}")
def delete_scene(scene_id: int):
    """删除场景（word_scenes/scene_collocations 由 ON DELETE CASCADE 联动删除）。"""
    conn = get_db()
    try:
        s = conn.execute("SELECT id FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        if not s:
            raise HTTPException(404, "场景不存在")
        conn.execute("DELETE FROM scenes WHERE id = ?", (scene_id,))
        conn.commit()
        return {"scene_id": scene_id, "message": "场景已删除"}
    finally:
        conn.close()


@router.post("/api/scenes/{scene_id}/compile")
async def compile_scene(scene_id: int, request: Request):
    """场景批量编译：取该场景下所有单词，调批量编译生成连环画。
    Body: {panel_count?, theme_hint?, image_model?}"""
    body = await _safe_json(request)
    panel_count = _clamp_int(body.get("panel_count", 4), 3, 8, 4)
    theme_hint = str(body.get("theme_hint", "") or "")
    image_model = str(body.get("image_model", "") or "") or IMAGE_MODEL
    # 场景编译默认荒诞三连弹；显式传空串 '' 表示微电影风格（此时 panel_count 生效）
    style = body.get("style", "absurd")
    style = "absurd" if style is None else str(style).strip()

    conn = get_db()
    try:
        s = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        if not s:
            raise HTTPException(404, "场景不存在")

        words = conn.execute("""
            SELECT w.word FROM word_scenes ws
            JOIN words w ON w.id = ws.word_id
            WHERE ws.scene_id = ? ORDER BY w.word
        """, (scene_id,)).fetchall()
        word_list = [r["word"] for r in words]
        if not word_list:
            raise HTTPException(400, "该场景下没有单词，无法编译")

        # 场景批量编译：复用批量编译的 LLM 逻辑（荒诞/冲突固定 3 画面），场景名作为主题提示
        scene_theme = theme_hint or s["name_en"]
        if not consume_daily_quota("ai"):
            raise HTTPException(429, f"今日 AI 生成已达上限 ({DAILY_AI_LIMIT} 次)")
        story, usage = await call_deepseek(word_list, panel_count, scene_theme, style=style)

        # 防护：LLM 未返回任何画面时，直接报错而非写入空记录
        if not story.get("panels"):
            raise HTTPException(502, "AI 未能生成画面内容，请重试")

        gen_id = str(uuid.uuid4())
        actual_panel_count = len(story.get("panels", [])) or panel_count

        panels_json = []
        for i, p in enumerate(story.get("panels", [])):
            panels_json.append({
                "scene_index": i + 1,
                "round_label": p.get("round_label", ""),
                "scene_role": p.get("scene_role", ""),
                "sentence_en": p.get("sentence_en", ""),
                "sentence_zh": p.get("sentence_zh", ""),
                "target_words_in_scene": p.get("target_words_in_scene", []),
                "word_notes": p.get("word_notes", {}),
                "collocations": p.get("collocations", []),
                "image_prompt": p.get("image_prompt", ""),
                "image_url": "",
                "image_error": None,
            })

        # 预扣图片配额（生成前检查，避免超限后仍返回图片）
        panels = story.get("panels", [])
        image_ok_count = 0
        if panels and not consume_daily_quota("image", len(panels)):
            for p in panels_json:
                p["image_url"] = None
                p["image_error"] = f"今日文生图已达上限 ({DAILY_IMAGE_LIMIT} 次)"
        else:
            image_tasks = [
                generate_panel_image(p.get("image_prompt", ""), image_model, gen_id, i + 1, style=style)
                for i, p in enumerate(panels)
            ]
            if image_tasks:
                cfg = _get_image_model_config(image_model)
                if cfg.get("endpoint") == "multimodal":
                    # 旗舰档：串行（RPM=2，且每次耗时较长）
                    image_results = []
                    for t in image_tasks:
                        image_results.append(await t)
                else:
                    # 均衡/性价比档：并发
                    image_results = await asyncio.gather(*image_tasks)
            else:
                image_results = []
            for p, ir in zip(panels_json, image_results):
                p["image_url"] = ir["url"]
                p["image_error"] = ir["error"]
            image_ok_count = sum(1 for ir in image_results if ir["url"])

        # 拼接 body_en（与批量编译一致）
        full_body_en = " ".join(p.get("sentence_en", "") for p in panels_json)

        conn.execute("""
            INSERT INTO generations (id,words,panel_count,theme_hint,
                                     story_title,theme,story_synopsis,body_en,model,image_model,panels,
                                     polysemy_notes,included_words,missing_words,ending_moral,
                                     generation_type,style)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            gen_id,
            json.dumps(word_list, ensure_ascii=False),
            actual_panel_count,
            theme_hint,
            story.get("story_title", ""),
            story.get("theme", ""),
            story.get("story_synopsis", ""),
            full_body_en,
            DEEPSEEK_MODEL,
            image_model,
            json.dumps(panels_json, ensure_ascii=False),
            json.dumps(story.get("polysemy_notes", {}), ensure_ascii=False),
            json.dumps(story.get("included_words", []), ensure_ascii=False),
            json.dumps(story.get("missing_words", []), ensure_ascii=False),
            story.get("ending_moral", ""),
            "scene",
            style,
        ))
        conn.commit()

        return {
            "gen_id": gen_id,
            "scene_id": scene_id,
            "scene_name": s["name_en"],
            "word_count": len(word_list),
            "panel_count": actual_panel_count,
            "image_success_count": image_ok_count,
            "message": f"场景「{s['name_en']}」已编译 {len(word_list)} 词 → {actual_panel_count} 画面连环画（{style or '微电影'}），图片 {image_ok_count}/{actual_panel_count}",
        }
    finally:
        conn.close()