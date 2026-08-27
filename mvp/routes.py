"""
TOEIC MVP API 路由
===================
所有业务 API 路由，使用 FastAPI APIRouter。
"""

import asyncio
import json
import random
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from config import *
from db import *
from services import *
from auth import get_current_user, require_quota

logger = setup_stream_logger("toeic.routes")

router = APIRouter(dependencies=[Depends(get_current_user)])

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


def _resolve_image_model(raw) -> str:
    """严格校验文生图模型：用户选什么就是什么，绝不静默兜底/替换到其他模型。
    未传、空或非法值一律明确报错，由前端提示用户重新选择。"""
    model = (raw or "").strip()
    if not model:
        raise HTTPException(400, "请选择文生图模型")
    for m in IMAGE_MODELS:
        if m["value"] == model:
            return model
    raise HTTPException(400, f"未知的文生图模型: {model}，请选择有效模型")


# ========================================================================
# SSE 进度透明：把每步"实际调用了哪个模型/状态"实时推给前端
# 生成流程以异步生成器 yield (event, data) 二元组；同步接口收集 result，
# 流式接口逐条转成 SSE 事件。
# ========================================================================

def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _image_provider_label(model: str) -> str:
    """返回文生图模型所属平台（dashscope/tokenrhythm），用于进度展示。"""
    return _get_image_model_config(model).get("provider", "dashscope")


def _llm_route_model(route_key: str) -> str:
    """返回某 LLM 调用点当前选定的模型名（用于 SSE 进度展示与入库）。"""
    return get_route_llm(route_key).get("model", "")


async def _consume_result(gen):
    """消费生成器，取最后一个 result 事件的数据（供同步接口用）。"""
    result = None
    async for evt, data in gen:
        if evt == "result":
            result = data
    return result


async def _sse_stream(gen):
    """把 (event, data) 生成器转成 SSE 文本流。"""
    async for evt, data in gen:
        yield _sse(evt, data)

def _delete_generation(gen_id: str, not_found_msg: str = "记录不存在") -> dict:
    """删除生成记录及其关联的音频和图片文件（先删库记录，成功后再清理文件）。"""
    conn = get_db()
    try:
        gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
        if not gen:
            raise HTTPException(404, not_found_msg)
        audio_files = [a["file_name"] for a in conn.execute("SELECT file_name FROM audios WHERE generation_id=?", (gen_id,)).fetchall()]
        image_names = [Path(p["image_url"]).name for p in json.loads(gen["panels"] or "[]") if p.get("image_url")]
        video_url = gen["video_url"] or ""
        conn.execute("DELETE FROM generations WHERE id=?", (gen_id,))
        conn.execute("DELETE FROM videos WHERE id=?", (gen_id,))
        conn.commit()
    finally:
        conn.close()
    for fn in audio_files:
        (AUDIOS_DIR / fn).unlink(missing_ok=True)
    for name in image_names:
        target = IMAGES_DIR / name
        if target.is_relative_to(IMAGES_DIR):
            target.unlink(missing_ok=True)
    if video_url:
        vname = Path(video_url).name
        vid_root = str(gen_id)
        for f in VIDEOS_DIR.glob(f"{vid_root}*.mp4"):
            f.unlink(missing_ok=True)
        (VIDEOS_DIR / vname).unlink(missing_ok=True)
    return {"ok": True}


async def _generate_audio(gen_id: str, voice: str, speed: float, tts_model: str, not_found_msg: str = "记录不存在", feature: str = "音频合成") -> dict:
    """为指定生成记录合成 TTS 音频（含去重、配额检查、落盘）。feature 用于日志标注调用来源功能。"""
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
        raise HTTPException(429, "今日 TTS 合成已达上限")

    audio_bytes = await call_tts(gen["body_en"], voice, speed, tts_model, feature=feature)
    # P1-2：文件名必须包含 tts_model（与去重唯一索引四字段对齐），否则切换模型重新生成会覆盖旧音频
    file_name = f"{gen_id}_{voice}_{int(speed*100)}_{tts_model}.mp3"
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

def _parse_generate_body(body: dict) -> dict:
    """解析并校验批量编译请求参数（同步/流式共用）。"""
    raw_words    = _coerce_str(body.get("words", ""))
    panel_count  = _clamp_int(body.get("panel_count", 4), 3, 8, 4)
    theme_hint   = body.get("theme_hint", "") or ""
    image_model  = _resolve_image_model(body.get("image_model"))
    generate_audio = _to_bool(body.get("generate_audio_immediately", False))
    tts_model    = body.get("tts_model", TTS_MODEL) if generate_audio else None
    tts_voice    = (body.get("tts_voice") or "").strip() if generate_audio else ""
    style        = body.get("style", "absurd")  # 缺省 'absurd'；显式传 '' 为旧版微电影
    art_style    = body.get("art_style", "")    # 可选画风，空=不指定
    track        = body.get("track", "general") # 语境赛道：general 通用 / tech 程序员
    if track not in ("general", "tech"):
        track = "general"

    words = normalize_words(raw_words)
    if not words:
        raise HTTPException(400, "请至少输入一个有效单词")
    if len(words) > 30:
        raise HTTPException(400, f"单次最多 30 个单词，当前 {len(words)} 个")
    if style not in ("absurd", "conflict") and panel_count not in (3, 4, 5):
        raise HTTPException(400, "画面数量只能是 3、4 或 5")
    return {
        "words": words, "panel_count": panel_count, "theme_hint": theme_hint,
        "image_model": image_model, "generate_audio": generate_audio, "tts_model": tts_model,
        "tts_voice": tts_voice,
        "style": style, "art_style": art_style, "track": track,
    }


async def _run_generate(p: dict):
    """批量编译核心流程（生成器）：LLM → 批量文生图 → 可选 TTS，逐步 yield 状态。"""
    words, panel_count = p["words"], p["panel_count"]
    theme_hint, image_model = p["theme_hint"], p["image_model"]
    generate_audio, tts_model = p["generate_audio"], p["tts_model"]
    style, art_style, track = p["style"], p["art_style"], p["track"]

    if not consume_daily_quota("ai"):
        raise HTTPException(429, "今日 AI 生成已达上限")

    yield ("step", {"step": "llm", "model": _llm_route_model("batch"), "label": "AI 生成剧情连环画", "status": "running"})
    gen_id = str(uuid.uuid4())[:8]
    result, usage = await call_deepseek(words, panel_count, theme_hint, style=style, art_style=art_style, track=track)
    actual_llm = result.pop("_llm_model", None) or _llm_route_model("batch")
    degraded = actual_llm != _llm_route_model("batch")
    yield ("step", {"step": "llm", "model": actual_llm, "label": "AI 生成剧情连环画", "status": "ok",
                    "message": f"选定模型不可用，已自动降级到 {actual_llm}" if degraded else ""})

    if not result.get("panels"):
        raise HTTPException(502, "AI 未能生成画面内容，请重试")

    actual_panel_count = len(result.get("panels", [])) or panel_count
    panels = result.get("panels", [])

    if len(panels) > 0 and not consume_daily_quota("image", len(panels)):
        raise HTTPException(429, "今日文生图已达上限")

    # 批量文生图：失败即整体中止（fail-fast），绝不静默替换模型或给出残缺结果
    if panels:
        yield ("step", {"step": "image", "model": image_model, "provider": _image_provider_label(image_model),
                        "label": f"生成 {len(panels)} 张图", "status": "running"})
        image_tasks = [
            generate_panel_image(p.get("image_prompt", ""), image_model, gen_id, p.get("scene_index", idx + 1), style=style, art_style=art_style, feature="批量编译")
            for idx, p in enumerate(panels)
        ]
        cfg = _get_image_model_config(image_model)
        if cfg.get("endpoint") == "multimodal":
            image_results = []
            for t in image_tasks:
                ir = await t
                image_results.append(ir)
                if not ir["url"]:
                    logger.error("批量文生图失败即中止 model=%s error=%r", image_model, ir.get("error"))
                    raise HTTPException(502, f"文生图模型 {image_model} 生成失败：{ir['error'] or '未知错误'}。请更换文生图模型后重试")
        else:
            image_results = await asyncio.gather(*image_tasks)
            for ir in image_results:
                if not ir["url"]:
                    logger.error("批量文生图失败 model=%s error=%r", image_model, ir.get("error"))
                    raise HTTPException(502, f"文生图模型 {image_model} 生成失败：{ir['error'] or '未知错误'}。请更换文生图模型后重试")
        for p, ir in zip(panels, image_results):
            p["image_url"] = ir["url"]
            p["image_error"] = ir["error"]
        yield ("step", {"step": "image", "model": image_model, "provider": _image_provider_label(image_model),
                        "label": f"生成 {len(panels)} 张图", "status": "ok"})
    else:
        image_results = []

    image_ok_count = sum(1 for ir in image_results if ir["url"])
    full_body_en = " ".join(p.get("sentence_en", "") for p in panels)

    conn = get_db()
    conn.execute("""
        INSERT INTO generations (id,words,panel_count,theme_hint,
                                 story_title,theme,story_synopsis,body_en,model,image_model,panels,
                                 polysemy_notes,included_words,missing_words,ending_moral,
                                 generation_type,style,track)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        gen_id, json.dumps(words), actual_panel_count, theme_hint,
        result.get("story_title", ""), result.get("theme", ""), result.get("story_synopsis", ""),
        full_body_en, actual_llm, image_model, json.dumps(panels),
        json.dumps(result.get("polysemy_notes", {})),
        json.dumps(result.get("included_words", [])),
        json.dumps(result.get("missing_words", [])),
        result.get("ending_moral", ""), "batch", style, track,
    ))
    for w in words:
        conn.execute("INSERT OR IGNORE INTO words(word) VALUES(?)", (w,))
    conn.commit()
    conn.close()

    resp = {
        "id": gen_id, "status": "success", "generation_type": "batch", "style": style, "track": track,
        "story_title": result.get("story_title", ""), "theme": result.get("theme", ""),
        "story_synopsis": result.get("story_synopsis", ""), "ending_moral": result.get("ending_moral", ""),
        "panels": panels, "words": words,
        "included_words": result.get("included_words", []),
        "missing_words": result.get("missing_words", []),
        "polysemy_notes": result.get("polysemy_notes", {}),
        "panel_count": actual_panel_count, "image_model": image_model,
        "image_success_count": image_ok_count, "has_audio": False, "audio_id": None,
    }

    if generate_audio and full_body_en:
        yield ("step", {"step": "tts", "model": tts_model, "label": "合成整段剧情音频", "status": "running"})
        if not consume_daily_quota("tts"):
            resp["audio_error"] = "今日 TTS 合成已达上限，未生成音频"
            yield ("step", {"step": "tts", "model": tts_model, "status": "failed", "message": resp["audio_error"]})
        else:
            try:
                voice = p.get("tts_voice") or default_tts_voice(tts_model)
                audio_bytes = await call_tts(full_body_en, voice, 1.0, tts_model, feature="批量编译音频")
                file_name = f"{gen_id}_{voice}_100_{tts_model}.mp3"
                (AUDIOS_DIR / file_name).write_bytes(audio_bytes)
                conn = get_db()
                cur = conn.execute(
                    "INSERT INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
                    (gen_id, file_name, voice, 1.0, tts_model),
                )
                conn.commit()
                conn.close()
                resp["has_audio"] = True
                resp["audio_id"] = cur.lastrowid
                resp["audio_url"] = f"/audios/{file_name}"
                resp["tts_model"] = tts_model
                yield ("step", {"step": "tts", "model": tts_model, "status": "ok"})
            except HTTPException as e:
                resp["audio_error"] = e.detail
                yield ("step", {"step": "tts", "model": tts_model, "status": "failed", "message": e.detail})
            except Exception as e:
                resp["audio_error"] = f"音频生成失败: {e}"
                yield ("step", {"step": "tts", "model": tts_model, "status": "failed", "message": str(e)})

    yield ("result", resp)


@router.post("/api/generate")
async def generate(req: Request):
    """批量编译（同步）：剧情生成 + 批量文生图 + 可选 TTS。"""
    body = await _safe_json(req)
    p = _parse_generate_body(body)
    require_quota("batch")
    return await _consume_result(_run_generate(p))


@router.post("/api/generate-stream")
async def generate_stream(req: Request):
    """批量编译（SSE 流式）：逐步推送实际调用的模型与状态。"""
    body = await _safe_json(req)
    p = _parse_generate_body(body)
    require_quota("batch")
    return StreamingResponse(_sse_stream(_run_generate(p)), media_type="text/event-stream")



@router.post("/api/generations/{gen_id}/audio")
async def generate_audio(gen_id: str, req: Request):
    body = await _safe_json(req)
    tts_model = body.get("tts_model", TTS_MODEL)
    voice = body.get("voice") or default_tts_voice(tts_model)
    speed = body.get("speed", 1.0)
    voice, speed = validate_tts_params(voice, speed)
    return await _generate_audio(gen_id, voice, speed, tts_model, "生成记录不存在", feature="剧情音频生成")


# ========================================================================
# 单点深耕 API
# ========================================================================

def _parse_single_body(body: dict) -> dict:
    """解析并校验单点深耕请求参数；非法值在此抛出（同步/流式共用）。"""
    import re as _re
    word_raw = (body.get("word") or "").strip().lower()
    theme_hint = body.get("theme_hint", "")
    image_model = _resolve_image_model(body.get("image_model"))
    generate_audio = _to_bool(body.get("generate_audio_immediately", False))
    tts_model = body.get("tts_model", TTS_MODEL) if generate_audio else None
    tts_voice = (body.get("tts_voice") or "").strip() if generate_audio else ""
    word_clean = _re.sub(r"[^a-zA-Z\-']", "", word_raw).lower()
    if not word_clean or len(word_clean) < 2:
        raise HTTPException(400, "请输入一个有效英文单词")
    art_style = body.get("art_style", "comic")
    track = body.get("track", "general")  # 语境赛道：general 通用 / tech 程序员
    if track not in ("general", "tech"):
        track = "general"
    return {
        "word": word_clean, "theme_hint": theme_hint, "image_model": image_model,
        "art_style": art_style, "generate_audio": generate_audio,
        "tts_model": tts_model, "tts_voice": tts_voice, "track": track,
    }


async def _run_single_compile(p: dict):
    """单点深耕核心流程（生成器）：LLM → 文生图 → 可选 TTS，逐步 yield 状态。"""
    word_clean, theme_hint = p["word"], p["theme_hint"]
    image_model, art_style = p["image_model"], p["art_style"]
    generate_audio, tts_model = p["generate_audio"], p["tts_model"]
    track = p["track"]

    if not consume_daily_quota("ai"):
        raise HTTPException(429, "今日 AI 生成已达上限")

    yield ("step", {"step": "llm", "model": _llm_route_model("single"), "label": "AI 生成记忆卡片", "status": "running"})
    gen_id = str(uuid.uuid4())[:8]
    result, usage = await call_deepseek_single(word_clean, theme_hint, art_style, track)
    actual_llm = result.pop("_llm_model", None) or _llm_route_model("single")
    degraded = actual_llm != _llm_route_model("single")
    yield ("step", {"step": "llm", "model": actual_llm, "label": "AI 生成记忆卡片", "status": "ok",
                    "message": f"选定模型不可用，已自动降级到 {actual_llm}" if degraded else ""})

    if not consume_daily_quota("image", 1):
        raise HTTPException(429, "今日文生图已达上限")
    yield ("step", {"step": "image", "model": image_model, "provider": _image_provider_label(image_model),
                    "label": "生成记忆钩子图", "status": "running"})
    ir = await generate_single_image(result.get("image_prompt", ""), image_model, gen_id, feature="单点深耕")
    image_url, image_error = ir["url"], ir["error"]
    if not image_url:
        logger.error("单点深耕文生图失败 model=%s error=%r", image_model, ir.get("error"))
        raise HTTPException(502, f"文生图模型 {image_model} 生成失败：{image_error or '未知错误'}。请更换文生图模型后重试")
    yield ("step", {"step": "image", "model": image_model, "provider": _image_provider_label(image_model),
                    "label": "生成记忆钩子图", "status": "ok"})

    scene_sentence = result.get("scene_sentence", {}) or {}
    body_en = scene_sentence.get("en", "") or ""
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
                                 generation_type,style,track)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        gen_id, json.dumps([word_clean]), 1, theme_hint,
        f"{word_clean} · 单点深耕", "单点深耕", scene_sentence.get("zh", ""), body_en,
        actual_llm, image_model,
        json.dumps(panels_payload, ensure_ascii=False),
        json.dumps({}, ensure_ascii=False),
        json.dumps([word_clean], ensure_ascii=False),
        json.dumps([], ensure_ascii=False),
        "", "single", "", track,
    ))
    meaning_zh = (result.get("meaning_zh") or "").strip()
    conn.execute("""
        INSERT INTO words(word, meaning_zh) VALUES(?, ?)
        ON CONFLICT(word) DO UPDATE SET meaning_zh = CASE WHEN COALESCE(words.meaning_zh, '') = '' THEN excluded.meaning_zh ELSE words.meaning_zh END
    """, (word_clean, meaning_zh))
    conn.commit()
    conn.close()

    resp = {
        "id": gen_id, "generation_type": "single", "status": "success",
        "word": word_clean, "meaning_zh": meaning_zh,
        "collocation": result.get("collocation", {}),
        "scene_sentence": scene_sentence, "image_prompt": result.get("image_prompt", ""),
        "hook_type": result.get("hook_type", ""), "image_url": image_url,
        "image_error": image_error, "derivatives": result.get("derivatives", []),
        "image_model": image_model, "art_style": art_style, "track": track,
        "has_audio": False, "audio_id": None,
    }

    if generate_audio and body_en:
        yield ("step", {"step": "tts", "model": tts_model, "label": "合成朗读音频", "status": "running"})
        if not consume_daily_quota("tts"):
            resp["audio_error"] = "今日 TTS 合成已达上限，未生成音频"
            yield ("step", {"step": "tts", "model": tts_model, "status": "failed", "message": resp["audio_error"]})
        else:
            try:
                voice = p.get("tts_voice") or default_tts_voice(tts_model)
                audio_bytes = await call_tts(body_en, voice, 1.0, tts_model, feature="单点深耕音频")
                file_name = f"{gen_id}_{voice}_100_{tts_model}.mp3"
                (AUDIOS_DIR / file_name).write_bytes(audio_bytes)
                conn = get_db()
                cur = conn.execute(
                    "INSERT INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
                    (gen_id, file_name, voice, 1.0, tts_model),
                )
                conn.commit()
                conn.close()
                resp["has_audio"] = True
                resp["audio_id"] = cur.lastrowid
                resp["audio_url"] = f"/audios/{file_name}"
                resp["tts_model"] = tts_model
                yield ("step", {"step": "tts", "model": tts_model, "status": "ok"})
            except HTTPException as e:
                resp["audio_error"] = e.detail
                yield ("step", {"step": "tts", "model": tts_model, "status": "failed", "message": e.detail})
            except Exception as e:
                resp["audio_error"] = f"音频生成失败: {e}"
                yield ("step", {"step": "tts", "model": tts_model, "status": "failed", "message": str(e)})

    yield ("result", resp)


@router.post("/api/single/compile")
async def single_compile(req: Request):
    """单点深耕（同步）：生成词伙 + 场景句 + 派生词 + 1 张记忆钩子图。"""
    body = await _safe_json(req)
    p = _parse_single_body(body)
    require_quota("single")
    return await _consume_result(_run_single_compile(p))


@router.post("/api/single/compile-stream")
async def single_compile_stream(req: Request):
    """单点深耕（SSE 流式）：逐步推送实际调用的模型与状态。"""
    body = await _safe_json(req)
    p = _parse_single_body(body)
    require_quota("single")
    return StreamingResponse(_sse_stream(_run_single_compile(p)), media_type="text/event-stream")



@router.post("/api/single/{gen_id}/audio")
async def single_generate_audio(gen_id: str, req: Request):
    """为单点深耕场景句生成朗读音频（后置生成）。"""
    body = await _safe_json(req)
    tts_model = body.get("tts_model", TTS_MODEL)
    voice = body.get("voice") or default_tts_voice(tts_model)
    speed = body.get("speed", 1.0)
    voice, speed = validate_tts_params(voice, speed)
    return await _generate_audio(gen_id, voice, speed, tts_model, "单点深耕记录不存在", feature="单点深耕朗读")


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
        "has_audio": bool(r["audio_file"]) or bool(r["video_url"]),
        "is_favorited": bool(r["is_favorited"]),
        "included_words": json.loads(r["included_words"] or "[]"),
        "missing_words": json.loads(r["missing_words"] or "[]"),
        "polysemy_notes": json.loads(r["polysemy_notes"] or "{}"),
        "first_image_url": (json.loads(r["panels"] or "[]")[0:1] or [{}])[0].get("image_url") if r["panels"] else None,
        "video_url": r["video_url"] or "",
    } for r in rows]


@router.get("/api/generations/{gen_id}")
async def get_generation(gen_id: str):
    conn = get_db()
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    aud = conn.execute("SELECT * FROM audios WHERE generation_id=? LIMIT 1", (gen_id,)).fetchone()
    narration_zh = ""
    video_model = gen["image_model"] if gen else ""
    if gen and (gen["generation_type"] == "video"):
        v = conn.execute("SELECT * FROM videos WHERE id=? LIMIT 1", (gen_id,)).fetchone()
        if v:
            narration_zh = v["narration_zh"] or ""
            video_model = v["model"] or video_model
    feedback = [r["rating"] for r in conn.execute(
        "SELECT rating FROM feedback WHERE generation_id=?", (gen_id,)
    ).fetchall()]
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
        "narration_zh": narration_zh,
        "video_model": video_model,
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
        "has_audio": bool(aud) or bool(gen["video_url"]),
        "tts_model": aud["tts_model"] if aud else None,
        "video_url": gen["video_url"] or "",
        "feedback": feedback,
    }


@router.post("/api/generations/{gen_id}/feedback")
async def submit_feedback(gen_id: str, req: Request):
    """记录/取消对某条生成结果的 👍/👎 反馈。"""
    body = await _safe_json(req)
    rating = (body.get("rating") or "").strip().lower()
    comment = (body.get("comment") or "").strip()[:500]  # 限制 500 字
    conn = get_db()
    exists = conn.execute("SELECT id FROM generations WHERE id=?", (gen_id,)).fetchone()
    conn.close()
    if not exists:
        raise HTTPException(404, "记录不存在")
    result = upsert_feedback(gen_id, rating, comment)
    return {"ok": True, "result": result, "stats": get_feedback_stats()}


@router.get("/api/feedback")
async def get_feedback():
    """反馈满意度统计（供查看用户对生成结果的满意度）。"""
    return get_feedback_stats()


# ========================================================================
# 平台看板（运维视图）：仅开发者/管理员可访问，聚合所有用户库的数据
# ========================================================================

def _require_staff():
    """反馈看板 / 全站历史等运维数据仅开发者与管理员可见；游客返回 403。"""
    user = current_user.get(None)
    if not user or user.get("role") not in ("dev", "admin"):
        raise HTTPException(403, "仅开发者/管理员可访问运维看板")


@router.get("/api/admin/dashboard")
async def admin_dashboard(days: int = 30):
    """反馈看板：跨库聚合活跃度 / 反馈 / 生成统计（dev/admin）。"""
    _require_staff()
    return collect_platform_dashboard(_clamp_int(days, 7, 90, 30))


@router.get("/api/admin/history")
async def admin_history(page: int = 1, page_size: int = 20, q: str = "",
                        rating: str = "", role: str = ""):
    """全站历史：所有用户的生成记录（分页 + 关键词 / 反馈 / 角色过滤，dev/admin）。"""
    _require_staff()
    return list_platform_history(
        page=_clamp_int(page, 1, 100000, 1),
        page_size=_clamp_int(page_size, 1, 100, 20),
        q=q, rating=rating, role=role,
    )


@router.delete("/api/generations/{gen_id}")
async def delete_generation(gen_id: str):
    return _delete_generation(gen_id, "记录不存在")


# ========================================================================
# 记忆测试（独立测试页）：Leitner 复习排期 + 作答日志
# 设计文档：开发过程文件/design-system/记忆测试-独立测试页开发文档.md
# ========================================================================

REVIEW_DAILY_LIMIT = 20                          # 每日复习量上限（防堆积，Anki 式）
REVIEW_BOX_INTERVALS = {1: 1, 2: 3, 3: 7, 4: 30}  # 盒号 -> 答对后下次复习间隔（天）
REVIEW_WORD_RE = re.compile(r"[^a-z\-']")        # 词形清洗（与 normalize_words 口径一致）


def _review_now() -> str:
    """本地时区 ISO 时间戳（不用 SQLite date('now')——其为 UTC，'次日'判定会漂移 8 小时）。"""
    return datetime.now().isoformat(timespec="seconds")


def _clean_review_word(raw) -> str:
    return REVIEW_WORD_RE.sub("", _coerce_str(raw).strip().lower())


def _review_transition(box: int, correct: bool):
    """Leitner 状态转移：答对升盒（盒4封顶续 30 天），答错回盒1。
    返回 (新盒号, 下次复习间隔天数)。导入盒 0 答对后到盒1（次日）。"""
    new_box = min(int(box) + 1, 4) if correct else 1
    return new_box, REVIEW_BOX_INTERVALS[new_box]


def _find_latest_single_gen(conn, word: str) -> str:
    """按词反查最新一张 single 卡 id（挂词不挂卡：队列只存词，素材按需反查）。
    generations.words 为 JSON 数组文本无索引，MVP 千级数据量全表扫无压力。"""
    rows = conn.execute(
        "SELECT id, words FROM generations WHERE generation_type='single' ORDER BY created_at DESC"
    ).fetchall()
    for r in rows:
        try:
            ws = [str(w).strip().lower() for w in json.loads(r["words"] or "[]")]
        except (json.JSONDecodeError, TypeError):
            ws = []
        if word in ws:
            return r["id"]
    return ""


def _load_review_material(conn, word: str, generation_id: str):
    """取词的卡片素材（panels[0]）。素材卡悬挂/为空时按词反查修复并落库；
    查无任何卡返回 (None, '')——该词跳过看图说词，仍可出中英匹配/挖空题。"""

    def _load(gen_id: str):
        if not gen_id:
            return None
        row = conn.execute("SELECT panels FROM generations WHERE id=?", (gen_id,)).fetchone()
        if not row:
            return None
        try:
            panels = json.loads(row["panels"] or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        return panels[0] if panels else None

    panel = _load(generation_id)
    if panel is not None:
        return panel, generation_id
    fixed = _find_latest_single_gen(conn, word)
    if fixed and fixed != generation_id:
        panel = _load(fixed)
        conn.execute(
            "UPDATE review_schedule SET generation_id=?, updated_at=? WHERE word=?",
            (fixed, _review_now(), word),
        )
        return panel, fixed
    return None, ""


def _pick_options(pool: list, correct: str, n: int, exclude: str = ""):
    """从干扰项池随机取 n 个不重复项 + 正确项，打乱返回。
    pool 已随机化（ORDER BY RANDOM），顺序取即随机取；不足 n 个时自然降级（直接回忆模式）。"""
    correct = (correct or "").strip()
    seen, distractors = {correct}, []
    for item in pool:
        v = (item or "").strip()
        if not v or v in seen or v == exclude:
            continue
        seen.add(v)
        distractors.append(v)
        if len(distractors) >= n:
            break
    options = distractors + [correct]
    random.shuffle(options)
    return options


@router.post("/api/review/import")
async def review_import(req: Request):
    """第一层卡片自测数据迁移落库。按 word upsert 去重：
    库中无该词 → 插入盒0、次日复习；已有 → 跳过保留进度。"""
    body = await _safe_json(req)
    items = body.get("items")
    if not isinstance(items, list):
        raise HTTPException(400, "items 必须是数组")
    imported, skipped, invalid = 0, 0, 0
    conn = get_db()
    try:
        for it in items:
            if not isinstance(it, dict):
                invalid += 1
                continue
            word = _clean_review_word(it.get("word", ""))
            if not word:
                invalid += 1
                continue
            if conn.execute("SELECT word FROM review_schedule WHERE word=?", (word,)).fetchone():
                skipped += 1
                continue
            gen_id = _find_latest_single_gen(conn, word)
            now = _review_now()
            next_at = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO review_schedule (word, generation_id, box, next_review_at, created_at, updated_at) "
                "VALUES (?,?,0,?,?,?)",
                (word, gen_id, next_at, now, now),
            )
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return {"imported": imported, "skipped": skipped, "invalid": invalid}


@router.get("/api/review/due")
async def review_due(override_limit: bool = False):
    """今日待复习列表：next_review_at <= now（含过期堆积），最老的优先，
    截断到每日上限（已答额度扣减）；截断不写库，顺延由次日仍到期自然实现。"""
    now = _review_now()
    conn = get_db()
    try:
        due_rows = conn.execute("""
            SELECT s.word, s.generation_id, s.box, s.next_review_at, s.lapses, s.correct_count,
                   COALESCE(w.meaning_zh, '') AS meaning_zh
            FROM review_schedule s LEFT JOIN words w ON w.word = s.word
            WHERE s.next_review_at <= ? AND (w.healed_at IS NULL OR w.healed_at = '')
            ORDER BY s.next_review_at ASC
        """, (now,)).fetchall()
        answered_today = conn.execute(
            "SELECT COUNT(*) c FROM review_log WHERE substr(answered_at,1,10)=?", (now[:10],)
        ).fetchone()["c"]
        total_due = len(due_rows)
        remaining = total_due if override_limit else max(0, REVIEW_DAILY_LIMIT - answered_today)
        items = []
        for r in due_rows[:remaining]:
            panel, gen_id = _load_review_material(conn, r["word"], r["generation_id"])
            colloc = ((panel or {}).get("collocation") or {})
            items.append({
                "word": r["word"],
                "meaning_zh": r["meaning_zh"] or "",
                "phrase_zh": (colloc.get("phrase_zh") or "").strip(),
                "box": r["box"],
                "next_review_at": r["next_review_at"],
                "lapses": r["lapses"],
                "correct_count": r["correct_count"],
                "generation_id": gen_id,
                "image_url": (panel or {}).get("image_url") or None,
                "has_image": bool(panel and (panel.get("image_url") or "").strip()),
            })
        # 干扰项池（整库随机中文义）：看图选义选项不足时由前端补充，
        # 避免单批到期词过少导致题目只剩正确选项、无干扰项。
        zh_pool = [r["meaning_zh"] for r in conn.execute(
            "SELECT meaning_zh FROM words WHERE meaning_zh != '' ORDER BY RANDOM() LIMIT 300"
        ).fetchall()]
        conn.commit()  # 素材反查修复落库
        return {
            "items": items,
            "total_due": total_due,
            "returned": len(items),
            "daily_limit": REVIEW_DAILY_LIMIT,
            "answered_today": answered_today,
            "override_limit": bool(override_limit),
            "zh_pool": zh_pool,
        }
    finally:
        conn.close()


@router.post("/api/review/answer")
async def review_answer(req: Request):
    """提交单次作答：按状态转移表更新盒子与排期 + 写作答日志（streak 数据源）。"""
    body = await _safe_json(req)
    word = _clean_review_word(body.get("word", ""))
    if not word:
        raise HTTPException(400, "word 不能为空")
    correct = _to_bool(body.get("correct"))
    qtype = _coerce_str(body.get("question_type", "")).strip()
    if qtype not in ("image_recall", "match", "cloze"):
        qtype = "image_recall"
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM review_schedule WHERE word=?", (word,)).fetchone()
        if not row:
            raise HTTPException(404, "该词不在复习队列中，请先在卡片自测中标记或刷新页面迁移")
        new_box, interval = _review_transition(row["box"], correct)
        now = _review_now()
        next_at = (datetime.now() + timedelta(days=interval)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE review_schedule SET box=?, next_review_at=?, lapses=lapses+?, "
            "correct_count=correct_count+?, updated_at=? WHERE word=?",
            (new_box, next_at, 0 if correct else 1, 1 if correct else 0, now, word),
        )
        conn.execute(
            "INSERT INTO review_log (word, result, question_type, answered_at) VALUES (?,?,?,?)",
            (word, "correct" if correct else "wrong", qtype, now),
        )
        conn.commit()
        return {
            "word": word,
            "correct": correct,
            "box": new_box,
            "next_review_at": next_at,
            "lapses": row["lapses"] + (0 if correct else 1),
            "correct_count": row["correct_count"] + (1 if correct else 0),
        }
    finally:
        conn.close()


@router.post("/api/review/heal")
async def review_heal(req: Request):
    """顽固词治愈自评：治愈 = 岛上多一棵树（words.healed_at 记录时间，自动退出待复习）；
    撤销治愈 = 重新回岛疗养（排期未动，自动回归复习队列）。判定权完全在用户自评。"""
    body = await _safe_json(req)
    word = _clean_review_word(body.get("word", ""))
    if not word:
        raise HTTPException(400, "word 不能为空")
    healed = _to_bool(body.get("healed", True))
    conn = get_db()
    try:
        row = conn.execute("SELECT word, healed_at FROM words WHERE word=?", (word,)).fetchone()
        if not row:
            raise HTTPException(404, "该词不在词库中")
        now = _review_now()
        if healed:
            if row["healed_at"]:
                return {"word": word, "healed": True, "healed_at": row["healed_at"], "changed": False}
            conn.execute("UPDATE words SET healed_at=? WHERE word=?", (now, word))
            conn.commit()
            return {"word": word, "healed": True, "healed_at": now, "changed": True}
        conn.execute("UPDATE words SET healed_at='' WHERE word=?", (word,))
        conn.commit()
        return {"word": word, "healed": False, "healed_at": "", "changed": bool(row["healed_at"])}
    finally:
        conn.close()


@router.get("/api/review/healed")
async def review_healed_list():
    """治愈图鉴：每个已治愈词一张病历卡 —— 上岛/治愈日期、疗养天数、
    作答统计与疗法清单（单点深耕 / 批量编译 / 场景聚汇 / 视频编译）。"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT w.word, w.pos, w.meaning_zh, w.phonetic, w.audio_url,
                   w.created_at, w.healed_at,
                   COALESCE(s.box, 0) AS box,
                   COALESCE(s.correct_count, 0) AS correct_count,
                   COALESCE(s.lapses, 0) AS lapses,
                   (SELECT COUNT(*) FROM review_log l WHERE l.word = w.word) AS answered_count
            FROM words w LEFT JOIN review_schedule s ON s.word = w.word
            WHERE w.healed_at IS NOT NULL AND w.healed_at != ''
            ORDER BY w.healed_at DESC
        """).fetchall()
        items = []
        for r in rows:
            w = r["word"]
            like = f'%"{w}"%'
            therapies = {"single": 0, "batch": 0, "scene": 0, "video": 0}
            for g in conn.execute(
                "SELECT generation_type FROM generations WHERE words LIKE ?", (like,)
            ).fetchall():
                t = g["generation_type"] or "batch"
                therapies[t] = therapies.get(t, 0) + 1
            therapies["video"] = conn.execute(
                "SELECT COUNT(*) c FROM videos WHERE words LIKE ?", (like,)
            ).fetchone()["c"]
            days = 0
            try:
                days = max(0, (date.fromisoformat(r["healed_at"][:10]) - date.fromisoformat(r["created_at"][:10])).days)
            except Exception:
                days = 0
            items.append({
                "word": w,
                "pos": r["pos"] or "",
                "meaning_zh": r["meaning_zh"] or "",
                "phonetic": r["phonetic"] or "",
                "audio_url": r["audio_url"] or "",
                "created_at": r["created_at"] or "",
                "healed_at": r["healed_at"] or "",
                "healed_days": days,
                "box": r["box"],
                "correct_count": r["correct_count"],
                "lapses": r["lapses"],
                "answered_count": r["answered_count"],
                "therapies": therapies,
            })
        total_days = sum(i["healed_days"] for i in items)
        return {
            "items": items,
            "count": len(items),
            "avg_days": round(total_days / len(items), 1) if items else 0,
        }
    finally:
        conn.close()


@router.get("/api/review/stats")
async def review_stats():
    """复习统计：盒子分布、正确率、连续打卡（review_log 按天聚合，非估算）。"""
    conn = get_db()
    try:
        now = _review_now()
        boxes = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        # 口径与 due/quiz 一致：治愈词已"出岛疗养"，不计入复习队列分布
        for r in conn.execute("""
            SELECT s.box, COUNT(*) c FROM review_schedule s
            LEFT JOIN words w ON w.word = s.word
            WHERE w.healed_at IS NULL OR w.healed_at = ''
            GROUP BY s.box
        """).fetchall():
            boxes[r["box"] if r["box"] in boxes else 0] += r["c"]
        answered_total = conn.execute("SELECT COUNT(*) c FROM review_log").fetchone()["c"]
        correct_total = conn.execute(
            "SELECT COUNT(*) c FROM review_log WHERE result='correct'"
        ).fetchone()["c"]
        answered_today = conn.execute(
            "SELECT COUNT(*) c FROM review_log WHERE substr(answered_at,1,10)=?", (now[:10],)
        ).fetchone()["c"]
        # streak：当日 ≥1 次作答即打卡；今天未打卡不算断，从昨天起算当前连续天数
        days = {r["d"] for r in conn.execute(
            "SELECT DISTINCT substr(answered_at,1,10) d FROM review_log"
        ).fetchall()}
        streak = 0
        cur = date.today()
        if cur.isoformat() not in days:
            cur = cur - timedelta(days=1)
        while cur.isoformat() in days:
            streak += 1
            cur = cur - timedelta(days=1)
        return {
            "total": sum(boxes.values()),
            "new": boxes[0],
            "in_progress": boxes[1] + boxes[2] + boxes[3],
            "mastered": boxes[4],
            "boxes": boxes,
            "healed": conn.execute(
                "SELECT COUNT(*) c FROM words WHERE healed_at IS NOT NULL AND healed_at != ''"
            ).fetchone()["c"],
            "due_now": conn.execute(
                """SELECT COUNT(*) c FROM review_schedule s
                   LEFT JOIN words w ON w.word = s.word
                   WHERE s.next_review_at<=? AND (w.healed_at IS NULL OR w.healed_at = '')""",
                (now,),
            ).fetchone()["c"],
            "answered_total": answered_total,
            "correct_total": correct_total,
            "accuracy": round(correct_total / answered_total, 4) if answered_total else 0.0,
            "answered_today": answered_today,
            "streak": streak,
            "daily_limit": REVIEW_DAILY_LIMIT,
        }
    finally:
        conn.close()


@router.get("/api/review/quiz")
async def review_quiz(count: int = 10, types: str = "image_recall,match,cloze"):
    """取一组自由测试题：到期词优先 + 未到期随机补足；题型轮换；
    素材降级链：看图说词(无图)→中英匹配(无中文)→场景句挖空(句不含词则跳过)。"""
    n = _clamp_int(count, 1, 50, 10)
    allowed_types = [t.strip() for t in types.split(",") if t.strip() in ("image_recall", "match", "cloze")]
    if not allowed_types:
        allowed_types = ["image_recall"]
    now = _review_now()
    conn = get_db()
    try:
        due_rows = conn.execute("""
            SELECT s.word, s.generation_id, s.box, COALESCE(w.meaning_zh, '') AS meaning_zh
            FROM review_schedule s LEFT JOIN words w ON w.word = s.word
            WHERE s.next_review_at <= ? AND (w.healed_at IS NULL OR w.healed_at = '')
            ORDER BY s.next_review_at ASC
        """, (now,)).fetchall()
        other_rows = conn.execute("""
            SELECT s.word, s.generation_id, s.box, COALESCE(w.meaning_zh, '') AS meaning_zh
            FROM review_schedule s LEFT JOIN words w ON w.word = s.word
            WHERE s.next_review_at > ? AND (w.healed_at IS NULL OR w.healed_at = '')
            ORDER BY RANDOM()
        """, (now,)).fetchall()
        pool = (due_rows + other_rows)[:n]
        if not pool:
            return {"questions": [], "total_words": 0}
        # 干扰项池（随机化）：词库非空中文释义（看图选义 / 中英匹配的选项）
        zh_pool = [r["meaning_zh"] for r in conn.execute(
            "SELECT meaning_zh FROM words WHERE meaning_zh != '' ORDER BY RANDOM() LIMIT 200"
        ).fetchall()]
        questions = []
        for i, r in enumerate(pool):
            word = r["word"]
            meaning = (r["meaning_zh"] or "").strip()
            panel, _gen_id = _load_review_material(conn, word, r["generation_id"])
            colloc = ((panel or {}).get("collocation") or {})
            scene = ((panel or {}).get("scene_sentence") or {})
            phrase_zh = (colloc.get("phrase_zh") or "").strip()
            qtype = allowed_types[i % len(allowed_types)]
            # 素材降级链（看图选义需"图 + 中文义"；无图或无中文退化为纯词题 match）
            if qtype == "image_recall" and (not ((panel or {}).get("image_url") or "").strip()
                                            or not (phrase_zh or meaning)):
                qtype = "match"
            if qtype == "match" and not (phrase_zh or meaning):
                qtype = "cloze"
            if qtype == "cloze":
                en = (scene.get("en") or "").strip()
                if not re.search(rf"\b{re.escape(word)}\b", en, re.IGNORECASE):
                    continue  # 场景句不含目标词（词形变化等），该词无题可出
                masked = re.sub(rf"\b{re.escape(word)}\b", "____", en, count=1, flags=re.IGNORECASE)
                questions.append({
                    "type": "cloze", "word": word, "box": r["box"],
                    "sentence_masked": masked, "sentence_en": en,
                    "sentence_zh": (scene.get("zh") or "").strip(),
                    "phrase_en": (colloc.get("phrase_en") or "").strip(),
                    "phrase_zh": phrase_zh, "meaning_zh": meaning,
                    "image_url": (panel or {}).get("image_url") or None,
                })
                continue
            if qtype in ("image_recall", "match"):
                # 看图选义 / 中英匹配：题干词或图，选正确中文义（优先卡片词伙，词库释义兜底）
                correct_zh = phrase_zh or meaning
                questions.append({
                    "type": qtype, "word": word, "box": r["box"],
                    "image_url": (panel or {}).get("image_url") or None,
                    "correct_zh": correct_zh,
                    "options": _pick_options(zh_pool, correct_zh, 3),
                    "sentence_en": (scene.get("en") or "").strip(),
                    "sentence_zh": (scene.get("zh") or "").strip(),
                    "phrase_en": (colloc.get("phrase_en") or "").strip(),
                    "phrase_zh": phrase_zh, "meaning_zh": meaning,
                })
        conn.commit()  # 素材反查修复落库
        return {"questions": questions, "total_words": len(pool)}
    finally:
        conn.close()


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
        "db": SYSTEM_DB_PATH.exists(),
        # 各模型通道密钥状态（供 manager 状态行/自检展示）
        "deepseek_key":      bool(DEEPSEEK_API_KEY),                     # LLM(DeepSeek 官方)
        "bailian_llm_key":   bool(IMAGE_API_KEY or TTS_API_KEY),         # LLM(百炼, 默认通道 Qwen3.7-Flash)
        "tts_key":           bool(TTS_API_KEY),                          # 语音合成
        "bailian_image_key": bool(IMAGE_API_KEY or TTS_API_KEY),         # 百炼文生图
        "tokenrhythm_key":   bool(TOKENRHYTHM_API_KEY),                  # TokenRhythm 免费文生图
        "image_key":         bool(IMAGE_API_KEY or TTS_API_KEY or TOKENRHYTHM_API_KEY),  # 文生图(任一通道)
        "video_key":         bool(VIDEO_API_KEY or IMAGE_API_KEY or TTS_API_KEY),        # 文生视频(百炼)
        "daily_usage": usage,
    }


@router.get("/api/usage")
async def usage_stats(days: int = 0):
    """返回模型调用统计（按日汇总、按模型汇总、最近明细），供用量情况页面展示。
    days=0（缺省）返回全部历史；days>0 仅返回近 days 天。
    开发者/管理员返回全站聚合（recent 带用户身份 + 用户用量排行榜），游客返回自己库的数据。"""
    user = current_user.get(None)
    if user and user.get("role") in ("dev", "admin"):
        return collect_platform_usage(days)
    return get_model_usage_stats(days)


# ========================================================================
# 文生图模型
# ========================================================================

@router.get("/api/image-models")
async def list_image_models():
    """返回文生图模型三档列表，供前端下拉选择。"""
    return {"models": IMAGE_MODELS}


@router.get("/api/video-models")
async def list_video_models():
    """返回文生视频模型列表（含免费额度标注），供前端下拉选择。"""
    return {"models": VIDEO_MODELS}


# ========================================================================
# 设置 API（LLM 模型路由）
# ========================================================================

@router.get("/api/settings/llm")
async def get_llm_settings():
    """返回可用的 LLM 模型列表与各调用点的当前选择，供设置页展示。"""
    from db import get_setting
    routes = []
    for r in LLM_ROUTES:
        current = get_setting(f"llm_route.{r['key']}", "") or r["default"]
        # 非法值回退默认
        if current not in LLM_MODEL_BY_VALUE:
            current = r["default"]
        routes.append({
            "key": r["key"],
            "label": r["label"],
            "desc": r["desc"],
            "default": r["default"],
            "current": current,
        })
    # 附带各模型是否已配置 key 的状态
    models = []
    for m in LLM_MODELS:
        models.append({**m, "configured": bool(m.get("api_key"))})
    return {"models": models, "routes": routes}


@router.post("/api/settings/llm")
async def save_llm_settings(req: Request):
    """保存各 LLM 调用点选择的模型（body: {routes: {route_key: model_value}}）。"""
    from db import set_setting
    body = await _safe_json(req)
    data = body.get("routes") or {}
    valid_keys = {r["key"] for r in LLM_ROUTES}
    valid_models = set(LLM_MODEL_BY_VALUE.keys())
    saved = 0
    errors = []
    for k, v in data.items():
        if k not in valid_keys:
            errors.append(f"未知调用点: {k}")
            continue
        if v not in valid_models:
            errors.append(f"调用点 {k} 的模型不合法: {v}")
            continue
        set_setting(f"llm_route.{k}", v)
        saved += 1
    return {"ok": True, "saved": saved, "errors": errors}


@router.get("/api/settings/tts")
async def get_tts_settings():
    """返回单词发音 TTS 模型设置：可用模型/音色清单与当前选择。"""
    from db import get_setting
    current = get_setting("tts_word_model", "") or TTS_MODEL
    if current not in TTS_MODEL_VALUES:
        current = TTS_MODEL
    current_voice = get_setting("tts_word_voice", "") or default_tts_voice(current)
    # 供前端下拉展示（含分组音色）
    group_map = {}
    for value in TTS_MODEL_VALUES:
        if value.startswith("qwen-audio"):
            g = "Qwen-Audio-TTS 系列"
        else:
            g = "CosyVoice 系列"
        voices = TTS_MODEL_VOICES.get(value, [])
        default_voice = default_tts_voice(value)
        voice_list = []
        for v in voices:
            if v == default_voice:
                continue
            note = TTS_VOICE_NOTES.get(v, "")
            voice_list.append({"value": v, "note": note})
        group_map.setdefault(g, []).append({
            "value": value,
            "default_voice": default_voice,
            "default_note": TTS_VOICE_NOTES.get(default_voice, ""),
            "voices": voice_list,
        })
    models = [{"group": g, "items": items} for g, items in group_map.items()]
    return {"models": models, "current": current, "current_voice": current_voice, "default": TTS_MODEL}


@router.post("/api/settings/tts")
async def save_tts_settings(req: Request):
    """保存单词发音 TTS 模型与音色（body: {tts_model, tts_voice}）。tts_voice 留空表示使用默认音色。"""
    from db import set_setting
    body = await _safe_json(req)
    value = (body.get("tts_model") or "").strip()
    if value not in TTS_MODEL_VALUES:
        raise HTTPException(400, f"不支持的 TTS 模型: {value}")
    voice = (body.get("tts_voice") or "").strip()
    if voice and voice not in (TTS_MODEL_VOICES.get(value, []) + [default_tts_voice(value)]):
        raise HTTPException(400, f"模型 {value} 不支持音色: {voice}")
    set_setting("tts_word_model", value)
    set_setting("tts_word_voice", voice)
    return {"ok": True, "tts_model": value, "tts_voice": voice or default_tts_voice(value)}


def _parse_video_body(body: dict) -> dict:
    """解析并校验视频编译请求参数（同步/流式共用）。"""
    raw_words   = _coerce_str(body.get("words", ""))
    theme_hint  = body.get("theme_hint", "") or ""
    video_model = body.get("video_model", "")
    duration    = _clamp_int(body.get("duration", 5), 2, 15, 5)
    tts_model   = body.get("tts_model", TTS_MODEL) or TTS_MODEL
    voice       = body.get("voice", "") or default_tts_voice(tts_model)
    art_style   = body.get("art_style", "") or ""
    track       = body.get("track", "general")  # 语境赛道：general 通用 / tech 程序员
    if track not in ("general", "tech"):
        track = "general"

    words = normalize_words(raw_words)
    if not words:
        raise HTTPException(400, "请至少输入一个有效单词")
    if len(words) > 30:
        raise HTTPException(400, f"单次最多 30 个单词，当前 {len(words)} 个")
    if not video_model:
        raise HTTPException(400, "请选择文生视频模型")
    if not _get_video_model_config(video_model):
        raise HTTPException(400, f"未知的文生视频模型: {video_model}，请选择有效模型")
    return {
        "words": words, "theme_hint": theme_hint, "video_model": video_model,
        "duration": duration, "tts_model": tts_model, "voice": voice, "art_style": art_style,
        "track": track,
    }


async def _run_video_generate(p: dict):
    """视频编译核心流程（生成器）：LLM 脚本 → 文生视频 → 配音/字幕，逐步 yield 状态。"""
    words, theme_hint = p["words"], p["theme_hint"]
    video_model, duration = p["video_model"], p["duration"]
    tts_model, voice, art_style = p["tts_model"], p["voice"], p["art_style"]
    track = p["track"]

    if not consume_daily_quota("ai"):
        raise HTTPException(429, "今日 AI 生成已达上限")

    vid_id = str(uuid.uuid4())[:8]
    conn = get_db()
    conn.execute(
        """INSERT INTO videos (id,words,theme_hint,model,duration,status)
           VALUES (?,?,?,?,?,'pending')""",
        (vid_id, json.dumps(words), theme_hint, video_model, duration),
    )
    conn.commit()
    conn.close()

    yield ("step", {"step": "llm", "model": _llm_route_model("video"), "label": "AI 编写视频脚本", "status": "running"})
    try:
        script, _ = await call_video_script(words, theme_hint, art_style, track)
    except HTTPException as e:
        # P2-1：脚本失败也回写 videos 状态，避免记录永久停留在 pending（与视频生成失败路径对齐）
        _update_video_status(vid_id, "failed", str(e.detail))
        raise
    except Exception as e:
        _update_video_status(vid_id, "failed", f"视频脚本生成失败: {e}")
        raise HTTPException(502, f"视频脚本生成失败: {e}")
    actual_llm = script.pop("_llm_model", None) or _llm_route_model("video")
    degraded = actual_llm != _llm_route_model("video")
    yield ("step", {"step": "llm", "model": actual_llm, "label": "AI 编写视频脚本", "status": "ok",
                    "message": f"选定模型不可用，已自动降级到 {actual_llm}" if degraded else ""})

    video_prompt = (script.get("video_prompt") or "").strip()
    if not video_prompt:
        raise HTTPException(502, "AI 未能生成视频提示词，请重试")

    yield ("step", {"step": "video", "model": video_model, "label": "生成视频（耗时约 1-5 分钟）", "status": "running"})
    try:
        video_bytes = await call_video_generation(video_prompt, video_model, duration)
    except Exception as e:
        _update_video_status(vid_id, "failed", str(e))
        raise HTTPException(502, f"视频生成失败: {e}")
    if not video_bytes:
        _update_video_status(vid_id, "failed", "视频生成返回空数据")
        raise HTTPException(502, "视频生成返回空数据")
    yield ("step", {"step": "video", "model": video_model, "label": "生成视频", "status": "ok"})

    narration_en = (script.get("narration_en") or "").strip()
    narration_zh = (script.get("narration_zh") or "").strip()
    raw_video_path = str(VIDEOS_DIR / f"{vid_id}_raw.mp4")
    (VIDEOS_DIR / f"{vid_id}_raw.mp4").write_bytes(video_bytes)

    final_name = f"{vid_id}.mp4"
    final_path = str(VIDEOS_DIR / final_name)
    if narration_en:
        yield ("step", {"step": "tts", "model": tts_model, "voice": voice, "label": "合成旁白并烧录字幕", "status": "running"})
        try:
            audio_bytes = await call_tts(narration_en, voice=voice, speed=1.0, model=tts_model, feature="视频配音")
            # P1-1：ffmpeg 编码为 CPU 密集同步操作（可达数十秒），放线程池执行避免阻塞事件循环，
            # 否则合成期间整个服务无响应（与 call_tts 的 executor 处理方式对齐）
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, mux_video_with_audio, raw_video_path, audio_bytes, narration_en, final_path
            )
            yield ("step", {"step": "tts", "model": tts_model, "status": "ok"})
        except Exception as e:
            _update_video_status(vid_id, "failed", f"配音/字幕合成失败: {e}")
            raise HTTPException(502, f"视频生成成功但配音/字幕合成失败: {e}")
    else:
        import shutil as _sh
        _sh.move(raw_video_path, final_path)

    (VIDEOS_DIR / f"{vid_id}_raw.mp4").unlink(missing_ok=True)

    conn = get_db()
    conn.execute(
        """UPDATE videos SET story_title=?, narration_en=?, narration_zh=?,
                             video_prompt=?, script=?, file_name=?, video_url=?, status='success'
           WHERE id=?""",
        (script.get("story_title", ""), narration_en, narration_zh, video_prompt,
         json.dumps(script), final_name, f"/videos/{final_name}", vid_id),
    )
    conn.execute(
        """INSERT INTO generations (id,words,panel_count,theme_hint,
                                     story_title,story_synopsis,body_en,model,image_model,panels,
                                     included_words,missing_words,generation_type,style,video_url,track)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (vid_id, json.dumps(words), duration, theme_hint,
         script.get("story_title", ""), narration_en[:80], narration_en,
         actual_llm, video_model, "[]",
         json.dumps(script.get("included_words", [])),
         json.dumps(script.get("missing_words", [])),
         "video", "video", f"/videos/{final_name}", track),
    )
    conn.commit()
    conn.close()

    yield ("result", {
        "id": vid_id, "status": "success", "generation_type": "video",
        "track": track,
        "story_title": script.get("story_title", ""),
        "narration_en": narration_en, "narration_zh": narration_zh,
        "video_prompt": video_prompt,
        "included_words": script.get("included_words", []),
        "missing_words": script.get("missing_words", []),
        "video_model": video_model, "duration": duration,
        "video_url": f"/videos/{final_name}",
        "has_audio": bool(narration_en), "tts_model": tts_model, "words": words,
    })


@router.post("/api/video/generate")
async def video_generate(req: Request):
    """视频编译（同步）：选词 → LLM 写视频脚本 → 百炼文生视频 → 存盘入库。"""
    body = await _safe_json(req)
    p = _parse_video_body(body)
    require_quota("video")
    return await _consume_result(_run_video_generate(p))


@router.post("/api/video/generate-stream")
async def video_generate_stream(req: Request):
    """视频编译（SSE 流式）：逐步推送实际调用的模型与状态。"""
    body = await _safe_json(req)
    p = _parse_video_body(body)
    require_quota("video")
    return StreamingResponse(_sse_stream(_run_video_generate(p)), media_type="text/event-stream")



def _update_video_status(vid_id: str, status: str, error: str = ""):
    conn = get_db()
    conn.execute("UPDATE videos SET status=?, error=? WHERE id=?", (status, error, vid_id))
    conn.commit()
    conn.close()


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
    frequency_level = _coerce_str(body.get("frequency_level", ""))
    conn = get_db()
    try:
        # 如果用户没填词性/释义/频率，用 LLM 自动补充
        if not pos or not meaning_zh or not frequency_level:
            enrich = await call_word_enrichment([word])
            if not enrich.get("skipped") and enrich.get("results"):
                for r in enrich["results"]:
                    if r["word"] == word:
                        if not pos:
                            pos = r["pos"]
                        if not meaning_zh:
                            meaning_zh = r["meaning_zh"]
                        if not frequency_level:
                            frequency_level = r["frequency_level"]
                        break
        cur = conn.execute(
            "INSERT INTO words (word, pos, meaning_zh, frequency_level, frequency_source) VALUES (?,?,?,?,'llm')",
            (word, pos, meaning_zh, frequency_level),
        )
        wid = cur.lastrowid
        # P2 推荐词导入词库：继承 word_root_links 暂存的频率/释义后清理
        inherit_link_frequency(conn, word)
        conn.commit()
        row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"单词 '{word}' 已存在")
    finally:
        conn.close()


@router.post("/api/words/single-stream")
async def create_word_stream(req: Request):
    """单条添加单词（SSE 流式）：缺词性/释义/频率时 AI 补全 → 入库，逐步反馈进度。"""
    body = await _safe_json(req)
    word = str(body.get("word", "")).strip().lower()
    if not word or len(word) < 2:
        raise HTTPException(400, "无效单词")
    pos = _coerce_str(body.get("pos", ""))
    meaning_zh = _coerce_str(body.get("meaning_zh", ""))
    frequency_level = _coerce_str(body.get("frequency_level", ""))
    require_quota("enrich")
    return StreamingResponse(
        _sse_stream(_run_single_add_stream(word, pos, meaning_zh, frequency_level)),
        media_type="text/event-stream",
    )


async def _run_single_add_stream(word: str, pos: str, meaning_zh: str, frequency_level: str):
    """单条添加单词核心流程（生成器）：缺少词性/释义/频率时先 AI 补充，再入库，逐步 yield。"""
    conn = get_db()
    row = None
    try:
        # 校验重复
        if conn.execute("SELECT 1 FROM words WHERE word=?", (word,)).fetchone():
            yield ("step", {"step": "check", "label": "检查重复", "status": "failed", "message": f"单词 '{word}' 已存在"})
            yield ("result", {"ok": False, "reason": "duplicated", "message": f"单词 '{word}' 已存在"})
            return

        # 缺词性/释义/频率时用 LLM 补充（成功入库前先判好，失败则中断，不写入）
        need_llm = (not pos) or (not meaning_zh) or (not frequency_level)
        if need_llm:
            enrich_model = _llm_route_model("enrich")
            yield ("step", {"step": "llm", "model": enrich_model, "label": "AI 补充词性释义", "status": "running"})
            try:
                enrich = await call_word_enrichment([word])
            except Exception as e:
                yield ("step", {"step": "llm", "model": enrich_model, "label": "AI 补充词性释义", "status": "failed", "message": f"补充失败：{e}"})
                yield ("result", {"ok": False, "reason": "llm_error", "message": f"AI 补充词性释义失败：{e}"})
                return
            enrich_ok = 0
            if not enrich.get("skipped") and enrich.get("results"):
                for r in enrich["results"]:
                    if str(r.get("word", "")).strip().lower() == word:
                        if not pos:
                            pos = _coerce_str(r.get("pos", ""))
                        if not meaning_zh:
                            meaning_zh = _coerce_str(r.get("meaning_zh", ""))
                        if not frequency_level:
                            frequency_level = _coerce_str(r.get("frequency_level", ""))
                        enrich_ok += 1
                        break
            if enrich_ok:
                yield ("step", {"step": "llm", "model": enrich_model, "label": "AI 补充词性释义", "status": "ok", "message": "补全完成"})
            else:
                yield ("step", {"step": "llm", "model": enrich_model, "label": "AI 补充词性释义", "status": "failed", "message": "AI 未返回有效补全结果"})

        yield ("step", {"step": "write", "label": f"写入单词 {word}", "status": "running"})
        cur = conn.execute(
            "INSERT INTO words (word, pos, meaning_zh, frequency_level, frequency_source) VALUES (?,?,?,?,'llm')",
            (word, pos, meaning_zh, frequency_level),
        )
        wid = cur.lastrowid
        # P2 推荐词导入词库：继承 word_root_links 暂存频率/释义后清理
        inherit_link_frequency(conn, word)
        conn.commit()
        yield ("step", {"step": "write", "label": f"写入单词 {word}", "status": "ok"})
        # 预生成发音缓存（无 TTS 配置或失败时不阻断存词）
        await _ensure_word_audio(word)
        row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    except sqlite3.IntegrityError:
        yield ("step", {"step": "write", "label": f"写入单词 {word}", "status": "failed", "message": f"单词 '{word}' 已存在"})
        yield ("result", {"ok": False, "reason": "duplicated", "message": f"单词 '{word}' 已存在"})
        return
    finally:
        conn.close()
    yield ("result", {"ok": True, "word": dict(row)})


@router.patch("/api/words/{word_id}")
async def update_word(word_id: int, req: Request):
    body = await _safe_json(req)
    allowed = {"pos", "meaning_zh", "phonetic"}
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


@router.post("/api/words/{word_id}/phonetic")
async def fetch_phonetic(word_id: int):
    """查询单词音标（调用免费词典 API），并存入数据库。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone()
        if not row:
            raise HTTPException(404, "单词不存在")
        word = row["word"]
        # 已有音标则直接返回
        if row["phonetic"]:
            return {"phonetic": row["phonetic"], "cached": True}

        phonetic = ""
        phonetic_error = ""
        # 1) 先尝试免费词典 API
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}")
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        phonetic = data[0].get("phonetic", "")
                        if not phonetic:
                            for p in data[0].get("phonetics", []):
                                text = p.get("text", "")
                                if text:
                                    phonetic = text
                                    break
        except Exception as e:
            phonetic_error = f"词典查询失败：{e}"

        # 2) 词典 API 无音标时，用 LLM 补全
        if not phonetic:
            try:
                phonetic = await call_word_phonetic(word)
            except Exception as e:
                phonetic_error = f"LLM 补全失败：{e}"

        conn.execute("UPDATE words SET phonetic=? WHERE id=?", (phonetic, word_id))
        conn.commit()
        return {"phonetic": phonetic, "cached": False, "error": phonetic_error or None}
    finally:
        conn.close()


@router.delete("/api/words/{word_id}")
async def delete_word(word_id: int):
    conn = get_db()
    # 同步清理复习排期，避免删除后留下孤儿 schedule 虚增复习统计
    # （撤销恢复时会按 box 0 重新入队，进度不丢入口）
    row = conn.execute("SELECT word FROM words WHERE id=?", (word_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM review_schedule WHERE word=?", (row["word"],))
    conn.execute("DELETE FROM words WHERE id=?", (word_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


async def _ensure_word_audio(word: str) -> str:
    """生成单个单词的 TTS 发音并缓存到 AUDIOS_DIR，返回 /audios/... URL。
    已有缓存直接返回；无 TTS 配置或生成失败返回空串（不抛出，不阻断调用方）。
    合成模型取自设置页「单词发音 TTS 模型」（tts_word_model），未配置时回退默认 TTS_MODEL。"""
    word = str(word or "").strip().lower()
    if not word:
        return ""
    from db import get_setting
    model = get_setting("tts_word_model", "") or TTS_MODEL
    voice = get_setting("tts_word_voice", "") or default_tts_voice(model)
    file_name = f"word_{word}_{voice}.mp3"
    url = f"/audios/{file_name}"
    # 已有缓存文件且数据库已记录则直接返回
    if (AUDIOS_DIR / file_name).exists():
        return url
    if not TTS_API_KEY:
        return ""
    try:
        audio_bytes = await call_tts(word, voice=voice, speed=1.0, model=model, feature="单词发音")
    except Exception as e:
        logger.warning("单词发音合成失败 [%s] error=%r", word, e)
        return ""
    consume_daily_quota("tts")
    (AUDIOS_DIR / file_name).write_bytes(audio_bytes)
    record_model_usage("tts", model, detail=f"单词发音 {word}", tokens=len(word))
    # 回填 words.audio_url（即时提交，避免写锁悬置）
    try:
        conn = get_db()
        conn.execute("UPDATE words SET audio_url=? WHERE word=?", (url, word))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return url


@router.post("/api/words/{word_id}/audio")
async def fetch_word_audio(word_id: int):
    """获取当前设置音色下的单词发音：命中该音色缓存直接返回，否则按当前音色生成并缓存。
    一个单词按音色隔离多个文件（word_{word}_{voice}.mp3），切换音色后按新音色重新判定，不复用旧音色。"""
    from db import get_setting
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone()
        if not row:
            raise HTTPException(404, "单词不存在")
        word = row["word"]
        # 按当前设置的音色解析目标文件名，命中对应音色缓存才直接返回；旧音色缓存不复用
        model = get_setting("tts_word_model", "") or TTS_MODEL
        voice = get_setting("tts_word_voice", "") or default_tts_voice(model)
        file_name = f"word_{word}_{voice}.mp3"
        if (AUDIOS_DIR / file_name).exists():
            return {"url": f"/audios/{file_name}", "cached": True}
        url = await _ensure_word_audio(word)
        if not url:
            raise HTTPException(500, f"发音生成失败：请检查 TTS_API_KEY 设置")
        return {"url": url, "cached": False}
    finally:
        conn.close()


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
        # 同步清理复习排期（与单个删除行为一致，避免孤儿 schedule 虚增统计）
        conn.execute(
            f"DELETE FROM review_schedule WHERE word IN "
            f"(SELECT word FROM words WHERE id IN ({placeholders}))",
            cleaned,
        )
        cur = conn.execute(f"DELETE FROM words WHERE id IN ({placeholders})", cleaned)
        deleted = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted, "count": len(cleaned)}


@router.post("/api/words/restore")
async def restore_words(req: Request):
    """撤销删除：重新插入被删的单词（已存在的自动忽略，恢复时保留原词性/释义/治愈状态）。
    治愈词恢复后仍为治愈（树上不缺树）；未治愈词若复习排期已随删除清理，按 box 0 重新入队。"""
    body = await _safe_json(req)
    words = body.get("words", []) or []
    conn = get_db()
    restored = 0
    try:
        now = _review_now()
        next_at = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        for item in words:
            if isinstance(item, dict):
                w = str(item.get("word", "")).strip().lower()
                pos = _coerce_str(item.get("pos", ""))
                meaning_zh = _coerce_str(item.get("meaning_zh", ""))
                healed_at = _coerce_str(item.get("healed_at", ""))
            else:
                w = str(item).strip().lower()
                pos, meaning_zh, healed_at = "", "", ""
            if not w or len(w) < 2:
                continue
            try:
                conn.execute(
                    "INSERT INTO words (word, pos, meaning_zh, healed_at) VALUES (?,?,?,?)",
                    (w, pos, meaning_zh, healed_at),
                )
                restored += 1
                # 未治愈词重新入队复习（box 0，次日到期）；治愈词不入队（保持治愈语义）
                if not healed_at:
                    conn.execute(
                        "INSERT OR IGNORE INTO review_schedule (word, box, next_review_at, created_at, updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (w, 0, next_at, now, now),
                    )
            except sqlite3.IntegrityError:
                continue
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "restored": restored}


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
        # P2 推荐词导入词库：继承 word_root_links 暂存频率/释义后清理
        for w in new_words:
            inherit_link_frequency(conn, w)
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
                        if r.get("pos") or r.get("meaning_zh") or r.get("frequency_level"):
                            conn.execute(
                                "UPDATE words SET pos=?, meaning_zh=?, frequency_level=CASE WHEN frequency_level='' THEN ? ELSE frequency_level END, frequency_source='llm' WHERE word=?",
                                (r.get("pos", ""), r.get("meaning_zh", ""), r.get("frequency_level", ""), r.get("word", "")),
                            )
                            enriched += 1
            conn.commit()
            # 预生成新词发音缓存（逐词串行，失败跳过不阻断导入）
            for w in new_words:
                await _ensure_word_audio(w)
    finally:
        conn.close()
    return {"imported": imported, "duplicated": duplicated, "total_input": len(word_list), "enriched": enriched}


async def _run_import_stream(word_list):
    """单词导入核心流程（生成器）：入库 → 分批 LLM 补全词性释义，逐步 yield 状态。"""
    cleaned = []
    for w in word_list:
        w = str(w).strip().lower()
        if w and len(w) >= 2:
            cleaned.append(w)
    if not cleaned:
        yield ("step", {"step": "import", "label": "写入单词", "status": "failed", "message": "未解析到有效单词"})
        yield ("result", {"imported": 0, "duplicated": 0, "total_input": len(word_list), "enriched": 0})
        return

    yield ("step", {"step": "import", "model": "", "label": f"写入 {len(cleaned)} 个单词", "status": "running"})
    conn = get_db()
    imported = 0
    duplicated = 0
    new_words = []
    try:
        for w in cleaned:
            try:
                conn.execute("INSERT INTO words (word) VALUES (?)", (w,))
                imported += 1
                new_words.append(w)
            except sqlite3.IntegrityError:
                duplicated += 1
        # P2 推荐词导入词库：继承 word_root_links 暂存频率/释义后清理
        for w in new_words:
            inherit_link_frequency(conn, w)
        conn.commit()
        yield ("step", {"step": "import", "model": "", "label": f"写入 {len(cleaned)} 个单词", "status": "ok",
                        "message": f"成功 {imported} 个，重复 {duplicated} 个"})

        enriched = 0
        if new_words:
            batch_size = 20
            total_batches = (len(new_words) + batch_size - 1) // batch_size
            for bi, i in enumerate(range(0, len(new_words), batch_size)):
                batch = new_words[i:i + batch_size]
                step_key = f"enrich_{bi}"
                step_label = f"AI 补充词性释义（第 {bi + 1}/{total_batches} 批）"
                yield ("step", {"step": step_key, "label": step_label, "status": "running"})
                try:
                    enrich = await call_word_enrichment(batch)
                except Exception:
                    yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": "该批补全失败，已跳过"})
                    continue
                ok = 0
                if not enrich.get("skipped") and enrich.get("results"):
                    for r in enrich["results"]:
                        if r.get("pos") or r.get("meaning_zh") or r.get("frequency_level"):
                            conn.execute(
                                "UPDATE words SET pos=?, meaning_zh=?, frequency_level=CASE WHEN frequency_level='' THEN ? ELSE frequency_level END, frequency_source='llm' WHERE word=?",
                                (r.get("pos", ""), r.get("meaning_zh", ""), r.get("frequency_level", ""), r.get("word", "")),
                            )
                            ok += 1
                conn.commit()
                enriched += ok
                yield ("step", {"step": step_key, "label": step_label, "status": "ok", "message": f"补全 {ok} 个"})
        # 预生成新词发音缓存（逐词串行，无 TTS 配置或失败跳过不阻断导入）
        if new_words:
            yield ("step", {"step": "tts", "model": TTS_MODEL, "label": f"生成 {len(new_words)} 个单词发音", "status": "running"})
            audio_ok = 0
            for w in new_words:
                if await _ensure_word_audio(w):
                    audio_ok += 1
            yield ("step", {"step": "tts", "model": TTS_MODEL, "label": "生成单词发音", "status": "ok", "message": f"成功 {audio_ok}/{len(new_words)} 个"})
    finally:
        conn.close()

    yield ("result", {"imported": imported, "duplicated": duplicated, "total_input": len(word_list), "enriched": enriched})


@router.post("/api/words/import-stream")
async def import_words_stream(req: Request):
    """单词导入（SSE 流式）：逐批反馈入库与 LLM 补全进度。"""
    body = await _safe_json(req)
    word_list = body.get("words", [])
    if not isinstance(word_list, list):
        word_list = []
    require_quota("enrich")
    return StreamingResponse(_sse_stream(_run_import_stream(word_list)), media_type="text/event-stream")


# ========================================================================
# 内容导入生岛：粘贴文章 → AI 提词 → 预览勾选 → 带释义上岛
# ========================================================================

@router.get("/api/island/stats")
async def island_stats():
    """词屿绿化总览：治愈一个词 = 岛上多一棵树。
    返回岛面规模（词库）、树木数（已治愈）、绿化阶段、航海日志（streak）、
    近期治愈词（最新 24 棵树，供前端在岛上按词渲染）。"""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM words").fetchone()["c"]
        healed_rows = conn.execute("""
            SELECT word, healed_at FROM words
            WHERE healed_at IS NOT NULL AND healed_at != ''
            ORDER BY healed_at DESC
        """).fetchall()
        in_review = conn.execute(
            "SELECT COUNT(*) c FROM review_schedule s LEFT JOIN words w ON w.word = s.word "
            "WHERE w.healed_at IS NULL OR w.healed_at = ''"
        ).fetchone()["c"]
        # streak：与 /api/review/stats 同口径（当日有作答即打卡）
        now = _review_now()
        days = {r["d"] for r in conn.execute(
            "SELECT DISTINCT substr(answered_at,1,10) d FROM review_log"
        ).fetchall()}
        streak = 0
        cur = date.today()
        if cur.isoformat() not in days:
            cur = cur - timedelta(days=1)
        while cur.isoformat() in days:
            streak += 1
            cur = cur - timedelta(days=1)
        # 绿化阶段：治愈数决定岛的形态
        healed = len(healed_rows)
        levels = [
            (0, "荒岛", "一座光秃秃的小岛，等第一批顽固词上岸"),
            (1, "新绿", "第一棵树生根了——治好的每个词都在岛上留下生命"),
            (5, "小树林", "树渐渐多起来，顽固词一个个被驯服"),
            (15, "绿洲", "岛上已成绿洲，你在语境记忆上走得很远"),
            (30, "茂密森林", "一座属于你的词汇森林——每棵树都是一个被打败的顽固词"),
        ]
        level_idx = 0
        for i, (threshold, _, _) in enumerate(levels):
            if healed >= threshold:
                level_idx = i
        next_threshold = levels[level_idx + 1][0] if level_idx + 1 < len(levels) else None
        return {
            "total_words": total,
            "healed": healed,
            "heal_ratio": round(healed / total, 4) if total else 0.0,
            "in_review": in_review,
            "streak": streak,
            "level": levels[level_idx][1],
            "level_desc": levels[level_idx][2],
            "next_level": levels[level_idx + 1][1] if level_idx + 1 < len(levels) else None,
            "next_threshold": next_threshold,
            "trees": [{"word": r["word"], "healed_at": r["healed_at"]} for r in healed_rows[:24]],
        }
    finally:
        conn.close()


@router.post("/api/island/extract-stream")
async def island_extract_stream(req: Request):
    """文章提词（SSE 流式）：LLM 从粘贴文章中提取值得学习的单词，
    返回词 + 词性 + 释义 + 原文语境句，供前端预览勾选（不写库）。"""
    body = await _safe_json(req)
    text = _coerce_str(body.get("text", ""))
    max_words = _clamp_int(body.get("max_words", 12), 3, 30, 12)
    if len(text.strip()) < 100:
        raise HTTPException(400, "文章太短了，至少粘贴 100 个字符的英文内容")

    async def _run():
        if not consume_daily_quota("ai"):
            raise HTTPException(429, "今日 AI 生成已达上限")
        yield ("step", {"step": "llm", "model": _llm_route_model("extract"), "label": "AI 阅读文章并提词", "status": "running"})
        result = await call_word_extraction(text, max_words)
        if result.get("skipped"):
            reason = result.get("reason", "")
            msg = {"no_api_key": "未配置可用的 LLM 模型", "parse_error": "AI 返回结果解析失败，请重试"}.get(reason, "AI 提词失败，请重试")
            yield ("step", {"step": "llm", "model": _llm_route_model("extract"), "label": "AI 阅读文章并提词", "status": "failed", "message": msg})
            raise HTTPException(502, msg)
        words = result["results"]
        if not words:
            yield ("step", {"step": "llm", "model": _llm_route_model("extract"), "label": "AI 阅读文章并提词", "status": "failed", "message": "这篇文章里没找到值得学习的单词"})
            yield ("result", {"words": [], "total": 0})
            return
        # 入库防线：过滤测试残留词/非法词，避免脏词进入提词候选
        words = [w for w in words if isinstance(w, dict) and is_clean_ai_word(str(w.get("word", "")))]
        # 标记词库已有词（已在岛上疗养中），供前端标注
        conn = get_db()
        try:
            existing = set(r["word"] for r in conn.execute("SELECT word FROM words").fetchall())
        finally:
            conn.close()
        for w in words:
            w["existing"] = w["word"] in existing
        if not words:
            yield ("step", {"step": "llm", "model": _llm_route_model("extract"), "label": "AI 阅读文章并提词", "status": "failed", "message": "这篇文章里没找到值得学习的单词"})
            yield ("result", {"words": [], "total": 0})
            return
        yield ("step", {"step": "llm", "model": _llm_route_model("extract"), "label": "AI 阅读文章并提词",
                        "status": "ok", "message": f"提取 {len(words)} 个值得学习的词"})
        yield ("result", {"words": words, "total": len(words)})

    return StreamingResponse(_sse_stream(_run()), media_type="text/event-stream")


@router.post("/api/island/confirm-stream")
async def island_confirm_stream(req: Request):
    """提词结果确认上岛（SSE 流式）：带 AI 已生成的词性释义直接入库（跳过重复补全），
    并为新词预生成 TTS 发音。语境句不入库——真正的语境疗养交给单点深耕/批量编译。"""
    body = await _safe_json(req)
    items = body.get("items", [])
    if not isinstance(items, list):
        items = []
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        w = _coerce_str(it.get("word", "")).strip().lower()
        if w and len(w) >= 2 and w not in [c["word"] for c in cleaned]:
            cleaned.append({
                "word": w,
                "pos": _coerce_str(it.get("pos", ""))[:20],
                "meaning_zh": _coerce_str(it.get("meaning_zh", ""))[:200],
            })
    if not cleaned:
        raise HTTPException(400, "未选择任何单词")
    require_quota("extract")

    async def _run():
        yield ("step", {"step": "import", "label": f"写入 {len(cleaned)} 个单词", "status": "running"})
        conn = get_db()
        imported, duplicated, new_words = 0, 0, []
        try:
            for it in cleaned:
                try:
                    conn.execute(
                        "INSERT INTO words (word, pos, meaning_zh) VALUES (?,?,?)",
                        (it["word"], it["pos"], it["meaning_zh"]),
                    )
                    imported += 1
                    new_words.append(it["word"])
                except sqlite3.IntegrityError:
                    duplicated += 1
            for w in new_words:
                inherit_link_frequency(conn, w)
            conn.commit()
            yield ("step", {"step": "import", "label": f"写入 {len(cleaned)} 个单词", "status": "ok",
                            "message": f"上岛 {imported} 个，已在岛上 {duplicated} 个"})
            if new_words:
                yield ("step", {"step": "tts", "model": TTS_MODEL, "label": f"生成 {len(new_words)} 个单词发音", "status": "running"})
                audio_ok = 0
                for w in new_words:
                    if await _ensure_word_audio(w):
                        audio_ok += 1
                yield ("step", {"step": "tts", "model": TTS_MODEL, "label": "生成单词发音", "status": "ok",
                                "message": f"成功 {audio_ok}/{len(new_words)} 个"})
        finally:
            conn.close()
        yield ("result", {"imported": imported, "duplicated": duplicated, "total": len(cleaned)})

    return StreamingResponse(_sse_stream(_run()), media_type="text/event-stream")


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
            "video_url": r["video_url"] or "",
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
    tts_model = body.get("tts_model", TTS_MODEL)
    voice = body.get("voice") or default_tts_voice(tts_model)
    speed = body.get("speed", 1.0)
    voice, speed = validate_tts_params(voice, speed)
    return await _generate_audio(text_id, voice, speed, tts_model, "文本不存在", feature="文本音频重新生成")


# ========================================================================
# 熟词僻意 API
# ========================================================================

@router.get("/api/polysemy")
async def get_polysemy(word: str = ""):
    if not word:
        raise HTTPException(400, "请提供单词")
    conn = get_db()
    row = conn.execute(
        """SELECT p.*, COALESCE(NULLIF(w.frequency_level,''), p.frequency_level) AS frequency_level
           FROM polysemy p LEFT JOIN words w ON w.word = p.word
           WHERE p.word=?""",
        (word.strip().lower(),),
    ).fetchone()
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
    rows = conn.execute(
        """SELECT p.*, COALESCE(NULLIF(w.frequency_level,''), p.frequency_level) AS frequency_level
           FROM polysemy p LEFT JOIN words w ON w.word = p.word
           ORDER BY LENGTH(REPLACE(COALESCE(NULLIF(w.frequency_level,''), p.frequency_level), '☆', '')) DESC,
                    frequency_level DESC
           LIMIT 20 OFFSET ?""",
        (offset,),
    ).fetchall()
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


@router.post("/api/polysemy/clean-suspicious")
async def clean_suspicious_data(user: dict = Depends(get_current_user)):
    """一键清理疑似测试残留数据（仅开发者/管理员）。
    对当前用户库执行数据体检：删除黑名单测试词 + 剥离释义中的会话残留元信息。"""
    if user["role"] not in ("dev", "admin"):
        raise HTTPException(403, "仅开发者/管理员可执行清理")
    conn = get_db()
    try:
        words_del, words_fix = [], []
        for r in conn.execute("SELECT id, word, meaning_zh FROM words ORDER BY id").fetchall():
            w = (r["word"] or "").strip().lower()
            if w in AI_WORD_BLACKLIST:
                words_del.append({"id": r["id"], "word": r["word"]})
                continue
            clean = clean_meaning_residue(r["meaning_zh"] or "")
            if clean != (r["meaning_zh"] or ""):
                words_fix.append({"id": r["id"], "word": r["word"], "after": clean})
        for it in words_del:
            conn.execute("DELETE FROM words WHERE id=?", (it["id"],))
        for it in words_fix:
            conn.execute("UPDATE words SET meaning_zh=? WHERE id=?", (it["after"], it["id"]))
        conn.commit()
    finally:
        conn.close()
    return {
        "deleted": [d["word"] for d in words_del],
        "fixed": [f["word"] for f in words_fix],
        "deleted_count": len(words_del),
        "fixed_count": len(words_fix),
    }


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
    require_quota("polysemy")

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
                "message": "今日 AI 生成已达上限，请明天再试。",
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
                # 入库防线：过滤测试残留词/非法词（黑名单 + 启发式校验）
                if not is_clean_ai_word(w):
                    rejected_words.append(w)
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


async def _run_polysemy_detect_stream(batch_size: int, max_batches: int):
    """熟词僻意自动检测核心流程（生成器）：分批 LLM 检测 → 入库，逐步 yield 状态。"""
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
        yield ("step", {"step": "candidates", "label": "扫描候选词", "status": "failed", "message": "没有新的候选词"})
        yield ("result", {
            "ok": True, "skipped_reason": "no_candidates",
            "message": "单词库中所有单词均已在熟词僻意表中，没有新的候选词。",
            "candidate_count": 0, "added_count": 0, "rejected_count": 0, "added_words": [], "rejected_words": [],
        })
        return

    yield ("step", {"step": "candidates", "label": f"扫描候选词（{len(candidates)} 个）", "status": "ok"})

    added_words = []
    rejected_words = []
    total_ai_cost = 0
    batches_processed = 0
    total_batches = min(max_batches, (len(candidates) + batch_size - 1) // batch_size)

    def _result(ok, reason=None, msg=""):
        return {
            "ok": ok, "skipped_reason": reason, "message": msg,
            "candidate_count": len(candidates), "batches_processed": batches_processed,
            "ai_quota_used": total_ai_cost,
            "added_count": len(added_words), "rejected_count": len(rejected_words),
            "added_words": added_words, "rejected_words": rejected_words,
        }

    for i in range(0, len(candidates), batch_size):
        if batches_processed >= max_batches:
            break
        batch = candidates[i:i + batch_size]
        if not batch:
            break
        batches_processed += 1
        step_key = f"llm_{batches_processed}"
        step_label = f"AI 判定熟词僻意（第 {batches_processed}/{total_batches} 批）"

        if not consume_daily_quota("ai"):
            yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": "今日 AI 生成已达上限"})
            yield ("result", _result(False, "ai_quota_exceeded", "今日 AI 生成已达上限，请明天再试。"))
            return
        total_ai_cost += 1
        yield ("step", {"step": step_key, "label": step_label, "status": "running"})

        try:
            detect_res = await call_polysemy_detection(batch)
        except HTTPException as e:
            yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": f"LLM 调用失败: {e.detail}"})
            yield ("result", _result(False, "llm_error", f"LLM 调用失败: {e.detail}"))
            return
        except Exception as e:
            yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": f"LLM 调用异常: {e}"})
            yield ("result", _result(False, "llm_error", f"LLM 调用异常: {e}"))
            return

        results = detect_res.get("results", [])
        conn = get_db()
        try:
            added = 0
            for item in results:
                w = item.get("word", "").strip().lower()
                if not w or w not in batch:
                    continue
                if item.get("is_polysemy") is True:
                    col = json.dumps(item.get("collocations") or [], ensure_ascii=False)
                    freq = str(item.get("frequency_level", ""))[:16]
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO polysemy
                               (word, common_meaning_zh, common_meaning_en,
                                business_meaning_zh, business_meaning_en,
                                example_en, example_zh, collocations,
                                toc_part, frequency_level)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (w, item.get("common_meaning_zh", ""), item.get("common_meaning_en", ""),
                             item.get("business_meaning_zh", ""), item.get("business_meaning_en", ""),
                             item.get("example_en", ""), item.get("example_zh", ""), col,
                             item.get("toc_part", ""), freq),
                        )
                        # 频率全局统一：一并写入 words（单词级单一事实来源）
                        conn.execute(
                            "UPDATE words SET frequency_level=CASE WHEN frequency_level='' THEN ? ELSE frequency_level END, frequency_source='llm' WHERE word=?",
                            (freq, w),
                        )
                        added_words.append(w)
                        added += 1
                    except Exception:
                        rejected_words.append(w)
                else:
                    rejected_words.append(w)
            conn.commit()
        finally:
            conn.close()
        yield ("step", {"step": step_key, "label": step_label, "status": "ok", "message": f"新增 {added} 个"})

    yield ("result", _result(True, None, (
        f"完成！共检测候选词 {len(candidates)} 个（{batches_processed} 批 / AI 配额消耗 {total_ai_cost} 次），"
        f"新增熟词僻意 {len(added_words)} 个，判定不匹配 {len(rejected_words)} 个。"
    )))


@router.post("/api/polysemy/auto-detect-stream")
async def polysemy_auto_detect_stream(req: Request):
    """熟词僻意自动检测（SSE 流式）：逐批反馈 LLM 判定与入库进度。"""
    body = await _safe_json(req)
    batch_size = _clamp_int(body.get("batch_size", 20), 5, 50, 20)
    max_batches = _clamp_int(body.get("max_batches", 5), 1, 20, 5)
    require_quota("polysemy")
    return StreamingResponse(
        _sse_stream(_run_polysemy_detect_stream(batch_size, max_batches)),
        media_type="text/event-stream",
    )


# ========================================================================
# 构词拆解 API（知识库 · 词根树）
# ========================================================================

MORPHEME_BATCH_SIZE = 20        # 每批送给 LLM 判定的词数
MORPHEME_SEED_PER_SCAN = 5      # 每次扫描最多懒填充的词根树数量（控配额）
MORPHEME_SEED_CAP = 15          # 每棵树 P2 推荐词软上限
MORPHEME_DIRECT_THRESHOLD = 40  # 候选词数 ≤ 该值时跳过启发式粗筛，直送 LLM 判定（避免漏掉真正可拆的待检词）
MORPHEME_MAX_PICK = 5           # 手动勾选拆解的单次词数上限（防止单批过大导致 LLM 超时）


def _freq_star_count(freq: str) -> int:
    """统计频率字符串里的星数（★~★★★★★），用于 P2 排序。"""
    return str(freq or "").count("★")


def _upsert_word_roots_for_item(conn, item: dict) -> int:
    """为可拆词建词根树节点，并把词挂到这些树（source=scan）。

    只对「词缀轴」建树（前缀 prefix / 后缀 suffix），词干（root）不单独成树，
    避免出现大量仅含 1 个词的空树。词干结构仍保存在 word_structures.morphemes 中，不丢失。
    返回本次新建的词根节点数。"""
    new_count = 0
    for m in (item.get("affixes", []) or []):
        mtype = str(m.get("type", "")).strip()
        if mtype not in ("prefix", "suffix"):
            continue
        affix = str(m.get("affix", "")).strip()
        if not affix:
            continue
        row = conn.execute("SELECT id FROM word_roots WHERE root=?", (affix,)).fetchone()
        if row:
            root_id = row["id"]
            conn.execute(
                "UPDATE word_roots SET root_zh=CASE WHEN root_zh='' THEN ? ELSE root_zh END, "
                "root_type=CASE WHEN root_type='' THEN ? ELSE root_type END WHERE id=?",
                (str(m.get("meaning", ""))[:80], mtype, root_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO word_roots (root, root_zh, root_type, sense) VALUES (?,?,?,?)",
                (affix, str(m.get("meaning", ""))[:80], mtype,
                 str(item.get("structure_code", ""))[:200]),
            )
            root_id = cur.lastrowid
            new_count += 1
        conn.execute(
            "INSERT OR IGNORE INTO word_root_links (word, root_id, source) VALUES (?,?,'scan')",
            (item["word"], root_id),
        )
    return new_count


def _morpheme_tree_members(conn, root_id: int) -> list[dict]:
    """取某词根树的全部成员词，排序：P1 已收录 > P2 推荐（内部按频率降序）。
    P0 已学暂缺数据源，预留优先级 0。

    「暂存 → 继承」口径：**在词库的词即 P1 已收录，频率以 words 为准**；不在词库的
    seed 词才是 P2 推荐，频率读 word_root_links 暂存值。这样 P2 词被导入词库后，
    自动升为已收录且频率读 words（不因 link 暂存被清空而丢失）。"""
    rows = conn.execute(
        """SELECT l.word, l.source, l.frequency_level AS link_freq, l.meaning_zh AS link_zh,
                  s.structure_code, w.frequency_level AS words_freq, w.meaning_zh AS words_zh,
                  (w.word IS NOT NULL) AS in_lib
           FROM word_root_links l
           LEFT JOIN word_structures s ON s.word = l.word
           LEFT JOIN words w ON w.word = l.word
           WHERE l.root_id = ?""",
        (root_id,),
    ).fetchall()
    members = []
    for r in rows:
        if r["in_lib"]:
            priority = 1
            freq = r["words_freq"] or r["link_freq"] or ""
            meaning = r["words_zh"] or r["link_zh"] or ""
        else:
            priority = 2
            freq = r["link_freq"] or ""
            meaning = r["link_zh"] or ""
        members.append({
            "word": r["word"],
            "priority": priority,
            "source": r["source"],
            "frequency_level": freq,
            "meaning_zh": meaning,
            "structure_code": r["structure_code"] or "",
        })
    members.sort(key=lambda m: (m["priority"], -_freq_star_count(m["frequency_level"]), m["word"]))
    return members


async def _run_morpheme_detect_stream(limit: int, force: bool, words: list[str] | None = None):
    """构词拆解扫描核心流程（生成器）：粗筛 → 分批 LLM 判定 → 建词根树 → 懒填充种子，逐步 yield。
    words 非空时表示手动勾选直送（跳过 force/limit/启发式），仅判定用户点选的词。"""
    conn = get_db()
    try:
        if words:
            cand_words = [str(w).strip().lower() for w in words if str(w).strip()]
        elif force:
            candidates = conn.execute(
                """SELECT w.word FROM words w
                   WHERE w.word IS NOT NULL AND w.word != ''
                   ORDER BY w.id LIMIT ?""",
                (limit,),
            ).fetchall()
            cand_words = [r["word"] for r in candidates]
        else:
            candidates = conn.execute(
                """SELECT w.word FROM words w
                   LEFT JOIN word_structures s ON s.word = w.word
                   WHERE s.word IS NULL AND w.word IS NOT NULL AND w.word != ''
                   ORDER BY w.id LIMIT ?""",
                (limit,),
            ).fetchall()
            cand_words = [r["word"] for r in candidates]
        # 启发式粗筛：命中内置词缀/前缀才进 LLM 确认名单（省 token，主推词缀轴）。
        # 但候选词较少时（如"待检查"的增量列表），硬切启发式会把真正可拆的词整批漏掉，因此 ≤ 阈值时直送 LLM。
        if len(cand_words) <= MORPHEME_DIRECT_THRESHOLD:
            rough = cand_words
        else:
            rough = [w for w in cand_words if hit_common_morpheme(w)]

        if not rough:
            yield ("step", {"step": "scan", "label": "扫描候选词", "status": "failed", "message": "没有命中常见词缀/词根的候选词"})
            yield ("result", {
                "ok": True, "skipped_reason": "no_candidates", "scanned": 0, "candidate_count": len(cand_words),
                "added_roots": 0, "added_words": [], "skipped": 0, "seeded_roots": 0,
                "message": "单词库中未命中常见词缀/词根的可拆候选词。",
            })
            return

        direct = words is not None or (len(rough) == len(cand_words) and len(cand_words) <= MORPHEME_DIRECT_THRESHOLD)
        label_kind = "手动" if words is not None else ("直送" if direct else "命中")
        yield ("step", {"step": "scan", "label": f"扫描候选词（{label_kind} {len(rough)}/{len(cand_words)} 个）", "status": "ok"})

        added_roots = 0
        added_words = []
        skipped = 0
        touched_root_ids = set()
        processed = 0
        total_batches = (len(rough) + MORPHEME_BATCH_SIZE - 1) // MORPHEME_BATCH_SIZE

        for i in range(0, len(rough), MORPHEME_BATCH_SIZE):
            batch = rough[i:i + MORPHEME_BATCH_SIZE]
            processed += 1
            step_key = f"llm_{processed}"
            step_label = f"AI 判定构词拆解（第 {processed}/{total_batches} 批）"

            if not consume_daily_quota("ai"):
                yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": "今日 AI 生成已达上限"})
                yield ("result", {"ok": False, "skipped_reason": "ai_quota_exceeded",
                                  "message": "今日 AI 生成已达上限，请明天再试。"})
                return
            yield ("step", {"step": step_key, "label": step_label, "status": "running"})

            try:
                detect_res = await call_morpheme_detect(batch)
            except HTTPException as e:
                yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": f"LLM 调用失败: {e.detail}"})
                yield ("result", {"ok": False, "skipped_reason": "llm_error", "message": f"LLM 调用失败: {e.detail}"})
                return
            except Exception as e:
                yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": f"LLM 调用异常: {e}"})
                yield ("result", {"ok": False, "skipped_reason": "llm_error", "message": f"LLM 调用异常: {e}"})
                return

            results = detect_res.get("results", [])
            model = str(detect_res.get("model", ""))
            batch_added = 0
            for item in results:
                w = str(item.get("word", "")).strip().lower()
                if not w or w not in batch:
                    continue
                # 入库防线：过滤测试残留词/非法词（黑名单 + 启发式校验）
                if not is_clean_ai_word(w):
                    continue
                if item.get("is_decomposable") is True:
                    morphemes = json.dumps({
                        "stem": item.get("stem", ""),
                        "stem_zh": item.get("stem_zh", ""),
                        "affixes": item.get("affixes", []),
                    }, ensure_ascii=False)
                    family = json.dumps(item.get("word_family", []), ensure_ascii=False)
                    conn.execute(
                        """INSERT OR REPLACE INTO word_structures
                           (word, structure_code, morphemes, word_family, is_decomposable, model)
                           VALUES (?,?,?,?,1,?)""",
                        (w, item.get("structure_code", ""), morphemes, family, model),
                    )
                    new_nodes = _upsert_word_roots_for_item(conn, item)
                    added_roots += new_nodes
                    # 记录本次触及的词根树（用于懒填充）
                    for rrow in conn.execute(
                        "SELECT root_id FROM word_root_links WHERE word=? AND source='scan'", (w,)
                    ).fetchall():
                        touched_root_ids.add(rrow["root_id"])
                    if w not in added_words:
                        added_words.append(w)
                    batch_added += 1
                else:
                    conn.execute(
                        """INSERT OR REPLACE INTO word_structures
                           (word, structure_code, morphemes, word_family, is_decomposable, model)
                           VALUES (?,'','{}','[]',0,?)""",
                        (w, model),
                    )
                    skipped += 1
            conn.commit()
            yield ("step", {"step": step_key, "label": step_label, "status": "ok",
                            "message": f"新增可拆 {batch_added} 个"})

        # 懒填充：本次触及且仍稀疏（真实收录 <2 且无种子）的词缀/前缀轴词根，补 3 个 P2 种子
        sparse_roots = []
        for rid in touched_root_ids:
            row = conn.execute(
                """SELECT r.*,
                          (SELECT COUNT(*) FROM word_root_links l WHERE l.root_id = r.id AND l.source='scan'
                             AND EXISTS (SELECT 1 FROM words w WHERE w.word = l.word)) AS real_count,
                          (SELECT COUNT(*) FROM word_root_links l WHERE l.root_id = r.id AND l.source='seed') AS seed_count
                   FROM word_roots r WHERE r.id = ?""",
                (rid,),
            ).fetchone()
            if not row:
                continue
            if row["root_type"] not in ("prefix", "suffix"):
                continue
            if row["real_count"] >= 2 or row["seed_count"] > 0:
                continue
            sparse_roots.append(row)
        sparse_roots.sort(key=lambda r: (r["real_count"], r["id"]))

        seeded_roots = 0
        for row in sparse_roots[:MORPHEME_SEED_PER_SCAN]:
            if not consume_daily_quota("ai"):
                break
            members = _morpheme_tree_members(conn, row["id"])
            existing = [m["word"] for m in members]
            step_key = f"seed_{seeded_roots + 1}"
            step_label = f"为词根 {row['root']} 推荐同构词"
            yield ("step", {"step": step_key, "label": step_label, "status": "running"})
            try:
                seed = await call_morpheme_seed(row["root"], row["root_zh"], row["root_type"], existing)
            except Exception as e:
                yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": f"推荐失败: {e}"})
                continue
            if not seed:
                yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": "推荐失败（模型无响应）"})
                continue
            ok = 0
            for rec in seed.get("recommended", []):
                w = str(rec.get("word", "")).strip().lower()
                if not w or w in existing:
                    continue
                if conn.execute("SELECT 1 FROM words WHERE word=?", (w,)).fetchone():
                    continue
                if conn.execute("SELECT 1 FROM word_root_links WHERE word=? AND root_id=?", (w, row["id"])).fetchone():
                    continue
                conn.execute("INSERT OR IGNORE INTO word_structures (word, is_decomposable) VALUES (?,1)", (w,))
                conn.execute(
                    "INSERT OR IGNORE INTO word_root_links (word, root_id, source, frequency_level, meaning_zh) VALUES (?,?,'seed',?,?)",
                    (w, row["id"], str(rec.get("frequency_level", ""))[:16], str(rec.get("meaning_zh", ""))[:200]),
                )
                ok += 1
            conn.commit()
            seeded_roots += 1
            yield ("step", {"step": step_key, "label": step_label, "status": "ok", "message": f"新增推荐 {ok} 个"})
    finally:
        conn.close()

    yield ("result", {
        "ok": True, "scanned": len(rough), "candidate_count": len(cand_words),
        "added_roots": added_roots, "added_words": added_words, "skipped": skipped,
        "seeded_roots": seeded_roots,
        "message": (
            f"完成！扫描 {len(rough)} 词，新增可拆词 {len(added_words)} 个、"
            f"新建词根节点 {added_roots} 个，判定不可拆 {skipped} 个，"
            f"为 {seeded_roots} 棵稀疏词根树补充了推荐词。"
        ),
    })


@router.post("/api/morphemes/detect-stream")
async def morphemes_detect_stream(request: Request):
    """构词拆解扫描（SSE 流式）：粗筛 → 分批 LLM 判定 → 建词根树 → 懒填充种子，逐步反馈进度。
    Body: {limit?: 扫描词数上限(默认50), force?: 是否全量重扫(默认false=增量检查), words?: 手动勾选直送(最多5)}"""
    body = await _safe_json(request)
    limit = _clamp_int(body.get("limit", 50), 1, 500, 50)
    force = _to_bool(body.get("force", False))
    words = None
    if isinstance(body.get("words"), list):
        words = [str(w).strip().lower() for w in body["words"] if str(w).strip()][:MORPHEME_MAX_PICK]
    return StreamingResponse(
        _sse_stream(_run_morpheme_detect_stream(limit, force, words)),
        media_type="text/event-stream",
    )


@router.get("/api/morphemes/roots")
async def list_morpheme_roots(page: int = 1, page_size: int = 12, search: str = ""):
    """词根树列表（分页），含各树收录/推荐词数量。"""
    page = max(1, page)
    page_size = _clamp_int(page_size, 6, 60, 12)
    conn = get_db()
    try:
        where, params = "", ()
        if search:
            where = "WHERE r.root LIKE ? OR r.root_zh LIKE ?"
            params = (f"%{search}%", f"%{search}%")
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT r.*,
                      (SELECT COUNT(*) FROM word_root_links l WHERE l.root_id = r.id AND l.source='scan') AS scan_count,
                      (SELECT COUNT(*) FROM word_root_links l WHERE l.root_id = r.id AND l.source='seed') AS seed_count
               FROM word_roots r {where}
               ORDER BY (scan_count + seed_count) DESC, r.id DESC
               LIMIT ? OFFSET ?""",
            params + (page_size, offset),
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM word_roots r {where}", params).fetchone()[0]
        items = [dict(r) for r in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        conn.close()


@router.get("/api/morphemes/roots/{root_id}")
async def get_morpheme_root(root_id: int):
    """单个词根树详情（同根词列表，朴素文字树）。"""
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM word_roots WHERE id=?", (root_id,)).fetchone()
        if not r:
            raise HTTPException(404, "词根不存在")
        root = dict(r)
        members = _morpheme_tree_members(conn, root_id)
        root["members"] = members
        root["scan_count"] = sum(1 for m in members if m["source"] == "scan")
        root["seed_count"] = sum(1 for m in members if m["source"] == "seed")
        return root
    finally:
        conn.close()


@router.post("/api/morphemes/roots/{root_id}/expand")
async def expand_morpheme_root(root_id: int, request: Request):
    """为某词根树追加 3 个 P2 推荐词（消耗 ai 配额 1 次；去重 + 软上限 15）。"""
    require_quota("morpheme")
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM word_roots WHERE id=?", (root_id,)).fetchone()
        if not r:
            raise HTTPException(404, "词根不存在")
        seed_count = conn.execute(
            "SELECT COUNT(*) c FROM word_root_links WHERE root_id=? AND source='seed'", (root_id,)
        ).fetchone()["c"]
        if seed_count >= MORPHEME_SEED_CAP:
            raise HTTPException(400, f"该词根已达推荐上限（{MORPHEME_SEED_CAP} 个），暂不可再添加")
        existing = [m["word"] for m in _morpheme_tree_members(conn, root_id)]
        if not consume_daily_quota("ai"):
            raise HTTPException(429, "今日 AI 生成已达上限")
        seed = await call_morpheme_seed(r["root"], r["root_zh"], r["root_type"], existing)
        if seed is None:
            raise HTTPException(500, "词根推荐生成失败，请稍后重试或更换模型")

        added, skipped = [], []
        for rec in seed.get("recommended", []):
            w = str(rec.get("word", "")).strip().lower()
            if not w:
                continue
            if w in existing:
                skipped.append(w)
                continue
            if conn.execute("SELECT 1 FROM words WHERE word=?", (w,)).fetchone():
                skipped.append(w)
                continue
            conn.execute("INSERT OR IGNORE INTO word_structures (word, is_decomposable) VALUES (?,1)", (w,))
            conn.execute(
                "INSERT OR IGNORE INTO word_root_links (word, root_id, source, frequency_level, meaning_zh) VALUES (?,?,'seed',?,?)",
                (w, root_id, str(rec.get("frequency_level", ""))[:16], str(rec.get("meaning_zh", ""))[:200]),
            )
            if w not in added:
                added.append(w)
        conn.commit()
        return {
            "ok": True, "added": added, "skipped": skipped,
            "reason_for_empty": seed.get("reason", "") if not added else "",
            "seed_total": seed_count + len(added),
        }
    finally:
        conn.close()


@router.get("/api/morphemes/words")
async def list_morpheme_words(page: int = 1, page_size: int = 20, search: str = ""):
    """已拆词列表（分页/搜索，仅可拆词）。"""
    page = max(1, page)
    page_size = _clamp_int(page_size, 5, 50, 20)
    conn = get_db()
    try:
        where, params = "WHERE s.is_decomposable=1 AND s.structure_code != ''", ()
        if search:
            where += " AND s.word LIKE ?"
            params = (f"%{search}%",)
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT s.*, COALESCE(NULLIF(w.frequency_level,''),'') AS frequency_level
                FROM word_structures s LEFT JOIN words w ON w.word = s.word
                {where} ORDER BY s.created_at DESC LIMIT ? OFFSET ?""",
            params + (page_size, offset),
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM word_structures s {where}", params).fetchone()[0]
        items = []
        for r in rows:
            d = dict(r)
            d["morphemes"] = json.loads(d.get("morphemes") or "{}")
            d["word_family"] = json.loads(d.get("word_family") or "[]")
            items.append(d)
        return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        conn.close()


@router.get("/api/morphemes")
async def get_morpheme(word: str = ""):
    """查询单个单词的构词结构（含挂载的词根树）。"""
    if not word:
        raise HTTPException(400, "请提供单词")
    w = word.strip().lower()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM word_structures WHERE word=?", (w,)).fetchone()
        if not row:
            raise HTTPException(404, "未收录该词的构词拆解")
        d = dict(row)
        d["morphemes"] = json.loads(d.get("morphemes") or "{}")
        d["word_family"] = json.loads(d.get("word_family") or "[]")
        d["roots"] = [dict(r) for r in conn.execute(
            "SELECT r.* FROM word_root_links l JOIN word_roots r ON r.id = l.root_id WHERE l.word=?", (w,)
        ).fetchall()]
        return d
    finally:
        conn.close()


@router.get("/api/morphemes/candidates")
async def morpheme_candidates():
    """返回词库中尚未做构词判定的候选词列表与数量（仅查询，不调用 LLM）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT w.id, w.word, w.pos, w.meaning_zh FROM words w
               LEFT JOIN word_structures s ON s.word = w.word
               WHERE s.word IS NULL ORDER BY w.id"""
        ).fetchall()
        return {
            "total": len(rows),
            "items": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@router.delete("/api/morphemes/words/{word}")
async def delete_morpheme_word(word: str):
    """删除一条构词拆解记录（word_root_links 由外键级联清理）。"""
    w = word.strip().lower()
    if not w:
        raise HTTPException(400, "请提供单词")
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM word_structures WHERE word=?", (w,))
        deleted = cur.rowcount or 0
        conn.commit()
        return {"ok": True, "deleted": deleted}
    finally:
        conn.close()


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
    require_quota("scene")

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
            raise HTTPException(429, "今日 AI 生成已达上限")
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
            # 每轮立即提交，释放写锁：避免 conn 的未提交写事务阻塞循环内 consume_daily_quota / record_model_usage
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


async def _run_scene_detect_stream(limit: int, force: bool):
    """场景自动检测核心流程（生成器）：扫描 → LLM 分类 → 写库 → 生成词伙，逐步 yield 状态。"""
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, name_en, name_zh, description FROM scenes WHERE status = 'active'"
        ).fetchall()
        existing_scenes = [dict(r) for r in existing]

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
            yield ("step", {"step": "scan", "label": "扫描待分类单词", "status": "failed", "message": "没有待分类的单词"})
            yield ("result", {"scanned": 0, "assigned_count": 0, "low_confidence_count": 0,
                              "new_scenes_suggested": [], "warning": "", "collocations_generated": 0,
                              "message": "没有待分类的单词"})
            return

        yield ("step", {"step": "scan", "label": f"扫描待分类单词（{len(words_to_assign)} 个）", "status": "ok"})

        if not consume_daily_quota("ai"):
            raise HTTPException(429, "今日 AI 生成已达上限")

        yield ("step", {"step": "llm", "model": _llm_route_model("batch"), "label": "AI 分类单词到场景", "status": "running"})
        result = await call_deepseek_scene_detect(words_to_assign, existing_scenes)
        assignments = result["scene_assignments"]
        new_scenes = result["new_scenes_suggested"]
        warning = result.get("warning", "")
        yield ("step", {"step": "llm", "model": _llm_route_model("batch"), "label": "AI 分类单词到场景", "status": "ok",
                        "message": f"归类 {len(assignments)} 词，建议新场景 {len(new_scenes)} 个"})

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

        collocations_generated = 0
        total_colloc = len(involved_scene_ids)
        for ci, sid in enumerate(involved_scene_ids):
            if not consume_daily_quota("ai"):
                break
            scene_row = conn.execute("SELECT name_en, name_zh FROM scenes WHERE id = ?", (sid,)).fetchone()
            if not scene_row:
                continue
            scene_words = [r["word"] for r in conn.execute(
                "SELECT w.word FROM word_scenes ws JOIN words w ON w.id = ws.word_id WHERE ws.scene_id = ?",
                (sid,),
            ).fetchall()]
            step_key = f"colloc_{ci}"
            step_label = f"生成词伙搭配（第 {ci + 1}/{total_colloc} 个场景）"
            yield ("step", {"step": step_key, "label": step_label, "status": "running"})
            try:
                cols = await call_deepseek_scene_collocations(scene_words, scene_row["name_en"], scene_row["name_zh"] or "")
            except Exception:
                yield ("step", {"step": step_key, "label": step_label, "status": "failed", "message": "该场景词伙生成失败，已跳过"})
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
            conn.commit()
            yield ("step", {"step": step_key, "label": step_label, "status": "ok", "message": f"生成 {len(cols)} 条"})
    finally:
        conn.close()

    msg = f"扫描 {len(words_to_assign)} 词，已归类 {assigned} 词，低置信度 {low_conf_count} 词，建议新场景 {len(new_scenes)} 个"
    if warning:
        msg = f"⚠️ {warning}"
    yield ("result", {
        "scanned": len(words_to_assign),
        "assigned_count": assigned,
        "low_confidence_count": low_conf_count,
        "new_scenes_suggested": new_scenes,
        "warning": warning,
        "collocations_generated": collocations_generated,
        "message": msg,
    })


@router.post("/api/scenes/detect-stream")
async def detect_scenes_stream(request: Request):
    """场景自动检测（SSE 流式）：扫描 → LLM 分类 → 词伙搭配，逐步反馈进度。"""
    body = await _safe_json(request)
    limit = _clamp_int(body.get("limit", 50), 1, 500, 50)
    force = _to_bool(body.get("force", False))
    return StreamingResponse(
        _sse_stream(_run_scene_detect_stream(limit, force)),
        media_type="text/event-stream",
    )


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


async def _run_scene_compile(scene_id: int, panel_count: int, theme_hint: str, image_model: str, art_style: str,
                             generate_audio: bool = False, tts_model: str = None, tts_voice: str = "",
                             track: str = "general"):
    """场景编译核心流程（生成器）：LLM → 批量文生图 → 可选 TTS，逐步 yield 状态。"""
    style = "scene"
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

        scene_theme = theme_hint or s["name_en"]
        scene_cols = conn.execute(
            "SELECT phrase_en FROM scene_collocations WHERE scene_id = ? ORDER BY created_at DESC",
            (scene_id,),
        ).fetchall()
        collocations = [r["phrase_en"] for r in scene_cols] if scene_cols else None
        if not consume_daily_quota("ai"):
            raise HTTPException(429, "今日 AI 生成已达上限")

        yield ("step", {"step": "llm", "model": _llm_route_model("batch"), "label": "AI 生成场景连环画", "status": "running"})
        story, usage = await call_deepseek(word_list, panel_count, scene_theme, style=style, collocations=collocations, art_style=art_style, track=track)
        actual_llm = story.pop("_llm_model", None) or _llm_route_model("batch")
        degraded = actual_llm != _llm_route_model("batch")
        yield ("step", {"step": "llm", "model": actual_llm, "label": "AI 生成场景连环画", "status": "ok",
                        "message": f"选定模型不可用，已自动降级到 {actual_llm}" if degraded else ""})

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

        panels = story.get("panels", [])
        image_ok_count = 0
        if panels and not consume_daily_quota("image", len(panels)):
            raise HTTPException(429, "今日文生图已达上限")
        elif panels:
            yield ("step", {"step": "image", "model": image_model, "provider": _image_provider_label(image_model),
                            "label": f"生成 {len(panels)} 张图", "status": "running"})
            image_tasks = [
                generate_panel_image(p.get("image_prompt", ""), image_model, gen_id, i + 1, style=style, art_style=art_style, feature="场景编译")
                for i, p in enumerate(panels)
            ]
            cfg = _get_image_model_config(image_model)
            if cfg.get("endpoint") == "multimodal":
                image_results = []
                for t in image_tasks:
                    ir = await t
                    image_results.append(ir)
                    if not ir["url"]:
                        logger.error("场景编译文生图失败即中止 model=%s error=%r", image_model, ir.get("error"))
                        raise HTTPException(502, f"文生图模型 {image_model} 生成失败：{ir['error'] or '未知错误'}。请更换文生图模型后重试")
            else:
                image_results = await asyncio.gather(*image_tasks)
                for ir in image_results:
                    if not ir["url"]:
                        logger.error("场景编译文生图失败 model=%s error=%r", image_model, ir.get("error"))
                        raise HTTPException(502, f"文生图模型 {image_model} 生成失败：{ir['error'] or '未知错误'}。请更换文生图模型后重试")
            for p, ir in zip(panels_json, image_results):
                p["image_url"] = ir["url"]
                p["image_error"] = ir["error"]
            image_ok_count = sum(1 for ir in image_results if ir["url"])
            yield ("step", {"step": "image", "model": image_model, "provider": _image_provider_label(image_model),
                            "label": f"生成 {len(panels)} 张图", "status": "ok"})

        full_body_en = " ".join(p.get("sentence_en", "") for p in panels_json)

        # 可选 TTS：编译完成后为整条场景剧情生成 TTS 听力音频（与其他编译功能对齐）
        audio_url, audio_error, actual_tts = "", "", ""
        if generate_audio and full_body_en:
            actual_tts = tts_model or TTS_MODEL
            yield ("step", {"step": "tts", "model": actual_tts, "label": "合成整段剧情音频", "status": "running"})
            if not consume_daily_quota("tts"):
                audio_error = "今日 TTS 合成已达上限，未生成音频"
                yield ("step", {"step": "tts", "model": actual_tts, "status": "failed", "message": audio_error})
            else:
                try:
                    voice = tts_voice or default_tts_voice(actual_tts)
                    audio_bytes = await call_tts(full_body_en, voice, 1.0, actual_tts, feature="场景编译音频")
                    file_name = f"{gen_id}_{voice}_100_{actual_tts}.mp3"
                    (AUDIOS_DIR / file_name).write_bytes(audio_bytes)
                    conn.execute(
                        "INSERT INTO audios (generation_id,file_name,voice,speed,tts_model) VALUES (?,?,?,?,?)",
                        (gen_id, file_name, voice, 1.0, actual_tts),
                    )
                    conn.commit()
                    audio_url = f"/audios/{file_name}"
                    yield ("step", {"step": "tts", "model": actual_tts, "status": "ok"})
                except HTTPException as e:
                    audio_error = e.detail
                    yield ("step", {"step": "tts", "model": actual_tts, "status": "failed", "message": e.detail})
                except Exception as e:
                    audio_error = f"音频生成失败: {e}"
                    yield ("step", {"step": "tts", "model": actual_tts, "status": "failed", "message": str(e)})

        conn.execute("""
            INSERT INTO generations (id,words,panel_count,theme_hint,
                                     story_title,theme,story_synopsis,body_en,model,image_model,panels,
                                     polysemy_notes,included_words,missing_words,ending_moral,
                                     generation_type,style,track)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            gen_id, json.dumps(word_list, ensure_ascii=False), actual_panel_count, theme_hint,
            story.get("story_title", ""), story.get("theme", ""), story.get("story_synopsis", ""),
            full_body_en, actual_llm, image_model,
            json.dumps(panels_json, ensure_ascii=False),
            json.dumps(story.get("polysemy_notes", {}), ensure_ascii=False),
            json.dumps(story.get("included_words", []), ensure_ascii=False),
            json.dumps(story.get("missing_words", []), ensure_ascii=False),
            story.get("ending_moral", ""), "scene", style, track,
        ))
        conn.commit()

        yield ("result", {
            "gen_id": gen_id, "scene_id": scene_id, "scene_name": s["name_en"],
            "word_count": len(word_list), "panel_count": actual_panel_count,
            "image_success_count": image_ok_count,
            "audio_url": audio_url, "audio_error": audio_error,
            "has_audio": bool(audio_url), "tts_model": actual_tts or None,
            "message": f"场景「{s['name_en']}」已编译 {len(word_list)} 词 → {actual_panel_count} 画面连环画（{style or '微电影'}），图片 {image_ok_count}/{actual_panel_count}",
        })
    finally:
        conn.close()


@router.post("/api/scenes/{scene_id}/compile")
async def compile_scene(scene_id: int, request: Request):
    """场景批量编译（同步）：取该场景下所有单词，调批量编译生成连环画。"""
    body = await _safe_json(request)
    panel_count = _clamp_int(body.get("panel_count", 4), 3, 8, 4)
    theme_hint = str(body.get("theme_hint", "") or "")
    image_model = _resolve_image_model(body.get("image_model"))
    art_style = body.get("art_style", "")  # 可选画风，空=不指定
    generate_audio = _to_bool(body.get("generate_audio_immediately", True))
    tts_model = body.get("tts_model", TTS_MODEL) if generate_audio else None
    tts_voice = (body.get("tts_voice") or "").strip() if generate_audio else ""
    track = body.get("track", "general")  # 语境赛道：general 通用 / tech 程序员
    if track not in ("general", "tech"):
        track = "general"
    require_quota("scene")
    return await _consume_result(_run_scene_compile(scene_id, panel_count, theme_hint, image_model, art_style,
                                                    generate_audio, tts_model, tts_voice, track))


@router.post("/api/scenes/{scene_id}/compile-stream")
async def compile_scene_stream(scene_id: int, request: Request):
    """场景批量编译（SSE 流式）：逐步推送实际调用的模型与状态。"""
    body = await _safe_json(request)
    panel_count = _clamp_int(body.get("panel_count", 4), 3, 8, 4)
    theme_hint = str(body.get("theme_hint", "") or "")
    image_model = _resolve_image_model(body.get("image_model"))
    art_style = body.get("art_style", "")  # 可选画风，空=不指定
    generate_audio = _to_bool(body.get("generate_audio_immediately", True))
    tts_model = body.get("tts_model", TTS_MODEL) if generate_audio else None
    tts_voice = (body.get("tts_voice") or "").strip() if generate_audio else ""
    track = body.get("track", "general")  # 语境赛道：general 通用 / tech 程序员
    if track not in ("general", "tech"):
        track = "general"
    require_quota("scene")
    return StreamingResponse(
        _sse_stream(_run_scene_compile(scene_id, panel_count, theme_hint, image_model, art_style,
                                       generate_audio, tts_model, tts_voice, track)),
        media_type="text/event-stream",
    )
