"""
TOEIC MVP 外部服务
==================
DeepSeek AI 生成、百炼 TTS 语音合成、百炼文生图。
"""

import asyncio
import json

import dashscope
import httpx
from dashscope.audio.http_tts import HttpSpeechSynthesizer
from fastapi import HTTPException

from config import *

__all__ = [
    "build_user_prompt",
    "call_deepseek",
    "call_tts",
    "call_image_generation",
    "generate_panel_image",
    "_get_image_model_config",
    "call_polysemy_detection",
    "call_word_enrichment",
]

# ========================================================================
# DeepSeek Prompt
# ========================================================================

SYSTEM_PROMPT = """You are a TOEIC Business English storyboard writer. Your audience is TOEIC test-takers who need to master stubborn vocabulary through a CINEMATIC STORY split into visual panels.

CORE IDEA: Pack the MOST target words into the SHORTEST possible sentences, tied together by ONE coherent story arc (setup → development → climax → resolution). Each panel = 1 high-density English sentence + 1 cinematic image description.

RULES:
1. Story must have a clear arc (e.g. friends pool money → debate a stock → invest → lose everything). Theme can be any business/workplace scenario (investment, negotiation, project failure, procurement, HR conflict, etc.).
2. Distribute ALL target words across the panels. Each panel packs 2-4 target words naturally via the scene (use common inflections if needed: reimburse→reimbursement). Never force-fit; if a word truly can't fit, list it in missing_words.
3. Each English sentence is SHORT and HIGH-DENSITY: 12-25 words, focused, memorable. Do NOT write long paragraphs.
4. Each panel's image_prompt must describe a CINEMATIC STORYBOARD frame (camera angle, lighting, composition, mood, 16:9). Keep visual continuity across panels (same characters, consistent style). image_prompt MUST be in English, 1-3 sentences.
5. Each panel gets a scene_role: setup|development|climax|resolution (distribute roles across panels).
6. Include word_notes (business meaning), collocations (business chunks), and polysemy_notes (words with special business meanings).
7. Output ONLY a valid JSON object. No markdown, no extra text.

JSON STRUCTURE:
{
  "story_title": "English title (3-8 words)",
  "theme": "Chinese theme description (e.g. 投资失败)",
  "story_synopsis": "Chinese one-sentence story summary",
  "panels": [
    {
      "scene_index": 1,
      "scene_role": "setup",
      "sentence_en": "One short high-density English sentence with 2-4 target words.",
      "sentence_zh": "Chinese translation of the sentence.",
      "target_words_in_scene": ["word1","word2"],
      "word_notes": {"word1": "中文商务释义", "word2": "中文商务释义"},
      "collocations": ["business collocation 1","business collocation 2"],
      "image_prompt": "Cinematic storyboard: [scene description in English]. [camera angle, lighting]. [mood]. 16:9, film grain."
    }
  ],
  "included_words": ["word1","word2"],
  "missing_words": [],
  "polysemy_notes": {"word": "explanation of its business meaning here"},
  "ending_moral": "Chinese one-sentence takeaway or lesson from the story."
}
"""


def build_user_prompt(words: list[str], panel_count: int = 4, theme_hint: str = ""):
    """构建 DeepSeek 用户提示词。"""
    words_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    theme_line = (
        f"\nTHEME HINT (optional, you may follow or override): {theme_hint}"
        if theme_hint
        else "\nTHEME: Choose any business/workplace scenario with a clear arc (investment, negotiation, project, procurement, HR, etc.). Be creative."
    )
    return f"""Please write a TOEIC business English CINEMATIC STORY split into {panel_count} visual panels.

TARGET WORDS ({len(words)} total):
{words_list}

PANEL COUNT: {panel_count} (must be exactly {panel_count} panels)
{theme_line}

CONSTRAINTS:
- Each panel's English sentence: 12-25 words, containing 2-4 target words.
- Distribute ALL {len(words)} target words across the {panel_count} panels as evenly as possible.
- Story must have a clear arc with setup/development/climax/resolution roles.
- image_prompt: cinematic storyboard style, 16:9, film grain, visual continuity across panels.
- All target words must appear naturally in business context.

Output only the JSON object."""


# ========================================================================
# DeepSeek AI 生成
# ========================================================================

async def call_deepseek(words: list[str], panel_count: int = 4, theme_hint: str = ""):
    """调用 DeepSeek API 生成剧情连环画。"""
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "请先设置 DEEPSEEK_API_KEY 环境变量")

    user_prompt = build_user_prompt(words, panel_count, theme_hint)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
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
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(content), data.get("usage", {})


# ========================================================================
# 百炼 TTS 语音合成
# ========================================================================

async def call_tts(text: str, voice=None, speed=1.0, model=None):
    """调用百炼 HTTP TTS API 合成语音，返回 mp3 二进制。"""
    if not TTS_API_KEY:
        raise HTTPException(500, "请先设置 TTS_API_KEY 环境变量")

    dashscope.api_key = TTS_API_KEY
    voice_name = voice or TTS_VOICE
    model_name = model or TTS_MODEL

    loop = asyncio.get_running_loop()
    try:
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

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            audio_resp = await client.get(result.audio_url)
            audio_resp.raise_for_status()
            return audio_resp.content
    except Exception as e:
        raise HTTPException(500, f"TTS 音频下载失败: {e}")


# ========================================================================
# 百炼文生图（三档模型：旗舰/均衡/性价比）
# ========================================================================

def _get_image_model_config(model_name: str) -> dict:
    """根据模型名返回其配置（端点类型等）。"""
    for m in IMAGE_MODELS:
        if m["value"] == model_name:
            return m
    return {"endpoint": "t2i", "price": "未知"}


async def _generate_image_qwen_multimodal(prompt: str, model: str) -> bytes:
    """旗舰档：qwen-image-3.0-pro / z-image-turbo，同步端点。"""
    size = "1024*1024" if model == "z-image-turbo" else "1664*928"
    url = f"{IMAGE_BASE_URL}/services/aigc/multimodal-generation/generation"
    payload = {
        "model": model,
        "input": {
            "messages": [{"role": "user", "content": [{"text": prompt}]}]
        },
        "parameters": {"size": size, "n": 1, "prompt_extend": True},
    }
    headers = {
        "Authorization": f"Bearer {IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    output = data.get("output", {})
    choices = output.get("choices", [])
    if not choices:
        raise RuntimeError(f"文生图返回无 choices: {data.get('message', '')}")
    content = choices[0].get("message", {}).get("content", [])
    image_url = None
    for c in content:
        if c.get("image"):
            image_url = c["image"]
            break
        if c.get("image_url"):
            image_url = c["image_url"]
            break
    if not image_url:
        raise RuntimeError("文生图返回无 image_url")

    async with httpx.AsyncClient(timeout=60.0) as client:
        img_resp = await client.get(image_url)
        img_resp.raise_for_status()
        return img_resp.content


async def _generate_image_wan_t2i(prompt: str, model: str) -> bytes:
    """均衡档：wan2.7-image，异步轮询端点。"""
    size = "1280*720"
    submit_url = f"{IMAGE_BASE_URL}/services/aigc/text2image/image-synthesis"
    payload = {
        "model": model,
        "input": {"prompt": prompt},
        "parameters": {"size": size, "n": 1},
    }
    headers = {
        "Authorization": f"Bearer {IMAGE_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(submit_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    task_id = data.get("output", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"文生图提交任务失败: {data.get('message', '')}")

    task_url = f"{IMAGE_BASE_URL}/tasks/{task_id}"
    headers_poll = {"Authorization": f"Bearer {IMAGE_API_KEY}"}
    for _ in range(40):
        await asyncio.sleep(3)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(task_url, headers=headers_poll)
            resp.raise_for_status()
            tdata = resp.json()
        status = tdata.get("output", {}).get("task_status", "")
        if status == "SUCCEEDED":
            results = tdata.get("output", {}).get("results", [])
            if not results:
                raise RuntimeError("文生图任务成功但无结果")
            image_url = results[0].get("url") or results[0].get("b64_image")
            if not image_url:
                raise RuntimeError("文生图结果无 url")
            break
        elif status == "FAILED":
            msg = tdata.get("output", {}).get("message", "未知错误")
            raise RuntimeError(f"文生图任务失败: {msg}")
    else:
        raise RuntimeError("文生图任务超时（120秒未完成）")

    async with httpx.AsyncClient(timeout=60.0) as client:
        img_resp = await client.get(image_url)
        img_resp.raise_for_status()
        return img_resp.content


async def call_image_generation(prompt: str, model: str = None) -> bytes:
    """文生图统一入口，按模型分派到同步或异步端点。返回图片二进制。"""
    if not IMAGE_API_KEY:
        raise HTTPException(500, "请先设置 IMAGE_API_KEY 或 TTS_API_KEY 环境变量")
    model_name = model or IMAGE_MODEL
    cfg = _get_image_model_config(model_name)
    try:
        if cfg.get("endpoint") == "multimodal":
            return await _generate_image_qwen_multimodal(prompt, model_name)
        else:
            return await _generate_image_wan_t2i(prompt, model_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"文生图失败 ({model_name}): {e}")


async def generate_panel_image(prompt: str, model: str, gen_id: str, scene_index: int) -> dict:
    """为单个画面生成图片，失败时降级（不阻塞整体）。"""
    full_prompt = f"cinematic storyboard, film grain, dramatic lighting, {prompt}, 16:9"
    try:
        img_bytes = await call_image_generation(full_prompt, model)
        file_name = f"{gen_id}_panel{scene_index}.png"
        file_path = IMAGES_DIR / file_name
        file_path.write_bytes(img_bytes)
        return {"url": f"/images/{file_name}", "file_name": file_name, "error": None}
    except Exception as e:
        return {"url": None, "file_name": None, "error": str(e.detail if hasattr(e, 'detail') else e)}


# ========================================================================
# 熟词僻意（Polysemy）自动检测
# ========================================================================

POLYSEMY_DETECT_SYSTEM = """You are a senior TOEIC vocabulary instructor specialized in identifying "familiar words with uncommon business meanings" (熟词僻意) that frequently appear in TOEIC Listening and Reading tests.

Your task: Given a list of candidate English words, for EACH word, determine whether it qualifies as a HIGH-FREQUENCY TOEIC POLYSEMY word — i.e., it has at least two distinct meanings, and the less common one is strongly associated with BUSINESS / WORKPLACE / COMMERCIAL contexts and frequently tested in TOEIC Part 5, 6, or 7.

Guidelines for a YES (is_polysemy = true):
- Word must have at least ONE clear "everyday / common" meaning (middle-school level or above)
- AND at least ONE distinct "business / formal" meaning that surprises average learners (e.g. address = "to deal with a problem", firm = "company", tender = "to submit a bid")
- AND that business meaning is frequently tested in TOEIC exams
- Examples that qualify: address, accommodate, charge, firm, issue, order, present, rate, share, term, bill, book, contract, credit, current, duty, figure, gross, line, margin, overhead, premium, return, security, stock, turnover, venture, yield, tender, etc.
- Examples that do NOT qualify: words with only 1 meaning (e.g. "receipt", "invoice", "conference" are business-only), or purely academic/technical words, or rare words. Words that are too simple (cat, run, go) should be rejected unless the business meaning is genuinely non-obvious to TOEIC learners.

Output ONLY a valid JSON object. No markdown. No extra text.

JSON STRUCTURE:
{
  "results": [
    {
      "word": "address",
      "is_polysemy": true,
      "common_meaning_zh": "地址",
      "common_meaning_en": "a location where a person or organization can be found or communicated with",
      "business_meaning_zh": "处理，解决（问题）；向…发表正式演说",
      "business_meaning_en": "to deal with a matter or problem formally; to deliver a speech to an audience",
      "example_en": "The manager will address the staff concerns in tomorrow's meeting.",
      "example_zh": "经理将在明天的会议上处理员工的关切。",
      "collocations": [
        "address an issue",
        "address a problem",
        "address the meeting",
        "address customer needs"
      ],
      "toc_part": "Part 5/6",
      "frequency_level": "★★★★★"
    },
    {
      "word": "chair",
      "is_polysemy": false
    }
  ]
}

Rules for each is_polysemy=true entry:
- common_meaning_zh / business_meaning_zh: concise Chinese, max 20 chars
- common_meaning_en / business_meaning_en: 5-20 words in natural English
- example_en: ONE natural TOEIC-style business sentence (12-25 words), must USE the word clearly in its BUSINESS meaning
- example_zh: accurate Chinese translation of the example
- collocations: 3-5 Chinese business collocations/chunks for the business meaning (English phrases)
- toc_part: Part 5, Part 6, Part 7, or combined like Part 5/6 — which TOEIC section(s) this word typically appears
- frequency_level: ★★★★★ very high, ★★★★☆ high, ★★★☆☆ medium, ★★☆☆☆ borderline/low
- If is_polysemy is false, omit all fields except "word" and "is_polysemy".
"""


def _build_polysemy_detect_prompt(words: list[str]) -> str:
    numbered = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    return f"""Please evaluate the following {len(words)} candidate words and determine if each is a HIGH-FREQUENCY TOEIC POLYSEMY word (熟词僻意).

CANDIDATE WORDS:
{numbered}

For each word:
1) Set "is_polysemy": true ONLY if the word has both a common everyday meaning AND a distinct business/workplace meaning that is frequently tested in TOEIC Part 5/6/7. Otherwise false.
2) For words where is_polysemy=true, fill ALL fields: common_meaning_zh, common_meaning_en, business_meaning_zh, business_meaning_en, example_en, example_zh, collocations (3-5), toc_part, frequency_level.
3) For words where is_polysemy=false, return ONLY word + is_polysemy=false, nothing else.

Return a single JSON object matching the schema provided."""


async def call_polysemy_detection(words: list[str]):
    """调用 DeepSeek 批量判断单词是否为托业高频熟词僻意，返回结构化词条。"""
    if not words:
        return {"results": []}
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "请先设置 DEEPSEEK_API_KEY 环境变量")

    user_prompt = _build_polysemy_detect_prompt(words)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": POLYSEMY_DETECT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
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
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(500, "LLM 返回格式非 JSON，无法解析熟词僻意结果")

    results = parsed.get("results", [])
    # 兜底：过滤掉格式错误的项
    cleaned = []
    seen = set()
    for r in results:
        w = str(r.get("word", "")).strip().lower()
        if not w or w in seen:
            continue
        seen.add(w)
        if r.get("is_polysemy") is True:
            cleaned.append({
                "word": w,
                "is_polysemy": True,
                "common_meaning_zh": str(r.get("common_meaning_zh", ""))[:100],
                "common_meaning_en": str(r.get("common_meaning_en", ""))[:200],
                "business_meaning_zh": str(r.get("business_meaning_zh", ""))[:100],
                "business_meaning_en": str(r.get("business_meaning_en", ""))[:200],
                "example_en": str(r.get("example_en", ""))[:400],
                "example_zh": str(r.get("example_zh", ""))[:400],
                "collocations": [str(c)[:100] for c in (r.get("collocations") or []) if isinstance(c, str)][:8],
                "toc_part": str(r.get("toc_part", ""))[:20],
                "frequency_level": str(r.get("frequency_level", ""))[:16],
            })
        else:
            cleaned.append({"word": w, "is_polysemy": False})
    return {"results": cleaned, "usage": data.get("usage", {})}


# ========================================================================
# 单词词性/释义自动补充
# ========================================================================

WORD_ENRICH_SYSTEM = """You are an English vocabulary assistant for TOEIC learners. Given a list of English words, return the part of speech and a comprehensive Chinese meaning for each word.

Rules:
- Part of speech (pos): use short labels like v., n., adj., adv., prep., conj., pron., etc. If a word has multiple common POS, list the most important ones separated by "/" (e.g. "v./n.").
- Chinese meaning (meaning_zh): provide a comprehensive yet concise Chinese definition. Include the most common meanings used in business/workplace contexts. Keep it under 80 characters.
- For words that are already in the input (e.g. inflected forms), return the base form's info.

Output ONLY a valid JSON object. No markdown, no extra text.

JSON STRUCTURE:
{
  "results": [
    {
      "word": "accommodate",
      "pos": "v.",
      "meaning_zh": "容纳；为…提供住宿；适应，顺应"
    },
    {
      "word": "negotiate",
      "pos": "v.",
      "meaning_zh": "谈判，协商；商议（条件）；顺利通过"
    }
  ]
}"""


def _build_enrich_prompt(words: list[str]) -> str:
    numbered = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    return f"""Please provide the part of speech and Chinese meaning for each of the following {len(words)} English words.

WORDS:
{numbered}

For each word, return:
- pos: part of speech label (e.g. v., n., adj., adv., or combined like "v./n.")
- meaning_zh: comprehensive Chinese definition (max 80 characters)

Return a single JSON object matching the schema provided."""


async def call_word_enrichment(words: list[str]) -> dict:
    """调用 DeepSeek 批量补充单词的词性和中文释义。"""
    if not words:
        return {"results": []}
    if not DEEPSEEK_API_KEY:
        return {"results": [], "skipped": True, "reason": "no_api_key"}

    user_prompt = _build_enrich_prompt(words)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": WORD_ENRICH_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
    except Exception:
        return {"results": [], "skipped": True, "reason": "llm_error"}

    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"results": [], "skipped": True, "reason": "parse_error"}

    results = parsed.get("results", [])
    cleaned = []
    seen = set()
    for r in results:
        w = str(r.get("word", "")).strip().lower()
        if not w or w in seen:
            continue
        seen.add(w)
        cleaned.append({
            "word": w,
            "pos": str(r.get("pos", ""))[:20],
            "meaning_zh": str(r.get("meaning_zh", ""))[:200],
        })
    return {"results": cleaned, "skipped": False}
