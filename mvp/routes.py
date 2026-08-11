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

def _delete_generation(gen_id: str, not_found_msg: str = "记录不存在") -> dict:
    """删除生成记录及其关联的音频和图片文件。"""
    conn = get_db()
    try:
        gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
        if not gen:
            raise HTTPException(404, not_found_msg)
        for a in conn.execute("SELECT file_name FROM audios WHERE generation_id=?", (gen_id,)).fetchall():
            (AUDIOS_DIR / a["file_name"]).unlink(missing_ok=True)
        for p in json.loads(gen["panels"] or "[]"):
            if p.get("image_url"):
                (IMAGES_DIR / p["image_url"].split("/")[-1]).unlink(missing_ok=True)
        conn.execute("DELETE FROM generations WHERE id=?", (gen_id,))
        conn.commit()
    finally:
        conn.close()
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
    cur = conn.execute(
        "INSERT INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
        (gen_id, file_name, voice, speed, tts_model),
    )
    audio_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {"id": audio_id, "generation_id": gen_id, "file_name": file_name,
            "url": f"/audios/{file_name}", "cached": False, "tts_model": tts_model}


# ========================================================================
# 生成 API
# ========================================================================

@router.post("/api/generate")
async def generate(req: Request):
    body = await req.json()
    raw_words    = body.get("words", "")
    panel_count  = int(body.get("panel_count", 4))
    theme_hint   = body.get("theme_hint", "")
    image_model  = body.get("image_model", IMAGE_MODEL)
    generate_audio = body.get("generate_audio_immediately", False)
    tts_model    = body.get("tts_model", TTS_MODEL) if generate_audio else None

    words = normalize_words(raw_words)
    if not words:
        raise HTTPException(400, "请至少输入一个有效单词")
    if len(words) > 30:
        raise HTTPException(400, f"单次最多 30 个单词，当前 {len(words)} 个")
    if panel_count not in (3, 4, 5):
        raise HTTPException(400, "画面数量只能是 3、4 或 5")

    if not consume_daily_quota("ai"):
        raise HTTPException(429, f"今日 AI 生成已达上限 ({DAILY_AI_LIMIT} 次)")

    gen_id = str(uuid.uuid4())[:8]
    result, usage = await call_deepseek(words, panel_count, theme_hint)

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
            generate_panel_image(p.get("image_prompt", ""), image_model, gen_id, p.get("scene_index", idx + 1))
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
                                 polysemy_notes,included_words,missing_words,ending_moral)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        gen_id,
        json.dumps(words),
        panel_count,
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
    ))
    for w in words:
        conn.execute("INSERT OR IGNORE INTO words(word) VALUES(?)", (w,))
    conn.commit()
    conn.close()

    resp = {
        "id": gen_id,
        "status": "success",
        "story_title": result.get("story_title", ""),
        "theme": result.get("theme", ""),
        "story_synopsis": result.get("story_synopsis", ""),
        "ending_moral": result.get("ending_moral", ""),
        "panels": panels,
        "words": words,
        "included_words": result.get("included_words", []),
        "missing_words": result.get("missing_words", []),
        "polysemy_notes": result.get("polysemy_notes", {}),
        "panel_count": panel_count,
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
    body = await req.json() if await req.body() else {}
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
    body = await req.json()
    word_raw = (body.get("word") or "").strip().lower()
    theme_hint = body.get("theme_hint", "")
    image_model = body.get("image_model", IMAGE_MODEL)
    generate_audio_immediately = body.get("generate_audio_immediately", False)
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
    body = await req.json() if await req.body() else {}
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
        "image_key": bool(IMAGE_API_KEY),
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
    body = await req.json()
    word = body.get("word", "").strip().lower()
    if not word or len(word) < 2:
        raise HTTPException(400, "无效单词")
    pos = body.get("pos", "")
    meaning_zh = body.get("meaning_zh", "")
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
    body = await req.json()
    allowed = {"pos", "meaning_zh"}
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


@router.delete("/api/words/{word_id}")
async def delete_word(word_id: int):
    conn = get_db()
    conn.execute("DELETE FROM words WHERE id=?", (word_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/api/words/parse")
async def parse_words(req: Request):
    body = await req.json()
    text = body.get("text", "")
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
    body = await req.json()
    word_list = body.get("words", [])
    conn = get_db()
    imported = 0
    duplicated = 0
    new_words = []
    for w in word_list:
        w = w.strip().lower()
        if not w or len(w) < 2:
            continue
        try:
            conn.execute("INSERT INTO words (word) VALUES (?)", (w,))
            imported += 1
            new_words.append(w)
        except sqlite3.IntegrityError:
            duplicated += 1
    # 批量调用 LLM 补充词性和释义
    if new_words:
        # 分批处理，每批最多 20 个
        batch_size = 20
        for i in range(0, len(new_words), batch_size):
            batch = new_words[i:i + batch_size]
            enrich = await call_word_enrichment(batch)
            if not enrich.get("skipped") and enrich.get("results"):
                for r in enrich["results"]:
                    if r["pos"] or r["meaning_zh"]:
                        conn.execute(
                            "UPDATE words SET pos=?, meaning_zh=? WHERE word=?",
                            (r["pos"], r["meaning_zh"], r["word"]),
                        )
        conn.commit()
    conn.close()
    return {"imported": imported, "duplicated": duplicated, "total_input": len(word_list)}


# ========================================================================
# 生成文本管理 API
# ========================================================================

@router.get("/api/texts")
async def list_texts(page: int = 1, search: str = "", favorited: int = 0, has_audio: int = 0):
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
        d["words"] = json.loads(d.get("words", "[]"))
        d["included_words"] = json.loads(d.get("included_words", "[]"))
        d["missing_words"] = json.loads(d.get("missing_words", "[]"))
        d["first_image_url"] = panels[0].get("image_url") if panels else None
        d["has_audio"] = bool(audio_map.get(r["id"]))
        d["audio_url"] = f"/audios/{audio_map[r['id']]}" if audio_map.get(r["id"]) else None
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": 20}


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
    body = await req.json()
    favorited = body.get("favorited", False)
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
    body = await req.json() if await req.body() else {}
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
        return None
    d = dict(row)
    d["collocations"] = json.loads(d.get("collocations", "[]"))
    return d


@router.get("/api/polysemy/hot")
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
    body = await req.json()
    words = body.get("words", []) or []
    cleaned = sorted({w.strip().lower() for w in words if isinstance(w, str) and w.strip()})
    if not cleaned:
        raise HTTPException(400, "请提供要删除的单词列表")
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
    body = await req.json() if await req.body() else {}
    batch_size = max(5, min(50, int(body.get("batch_size", 20))))
    max_batches = max(1, min(20, int(body.get("max_batches", 5))))

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