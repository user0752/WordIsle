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
    "call_deepseek_single",
    "build_single_user_prompt",
    "call_tts",
    "call_image_generation",
    "generate_panel_image",
    "generate_single_image",
    "_get_image_model_config",
    "call_polysemy_detection",
    "call_word_enrichment",
    "call_deepseek_scene_detect",
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
# 单点深耕 Prompt & 生成
# ========================================================================

SINGLE_SYSTEM_PROMPT = """You are a TOEIC Business English coach specialized in the "one word, one image, one hook" memorization technique.

CORE IDEA: Given ONE English word, produce a single ABSURD memory-hook image that COLLIDES the word's common/literal meaning with its business meaning in the SAME frame. The image itself becomes the recall trigger — when the user sees the picture, they instantly remember both meanings of the word.

## TASK
Given one English target word, output:
1. ONE high-frequency TOEIC collocation (e.g. submit → submit a proposal).
2. ONE absurd/contrasting/playing-with-convention scene sentence (English + Chinese), MUST contain the target word.
3. ONE image_prompt — the visual description MUST stuff the word's common meaning AND its business meaning into the SAME picture to create absurd contrast (NOT natural narrative).
4. The word's derivative family (noun/verb/adjective/adverb forms, with Chinese meanings).

## IMAGE REQUIREMENT (CRITICAL)
- image_prompt MUST create a "two-meanings collision", not natural storytelling.
- Example: tender (gentle + bid) → "A person cradling a sealed tender document with exaggeratedly tender/gentle hand gestures, like holding a baby, in a cold corporate boardroom. The contrast between the gentleness and the rigid business setting creates absurdity."
- Make the picture itself the recall cue: the user sees the image and is reminded of both meanings.

## RULES
1. scene_sentence.en: 10-20 words, contains the target word, business context, absurd/contrasting tone.
2. scene_sentence.mood: 2-3 Chinese tags describing the absurd tone (e.g. 荒诞 / 反差 / 黑色幽默).
3. derivatives: 2-4 items; if no common derivatives exist, return an empty array.
4. collocation_type: grammatical pattern (e.g. verb + noun, adj + noun, noun + noun).
5. image_prompt MUST be in English, 1-3 sentences, surreal comic / flat illustration style (NOT cinematic, NOT realistic).
6. Output ONLY a valid JSON object. No markdown, no extra text.

## JSON STRUCTURE
{
  "word": "submit",
  "collocation": {
    "phrase_en": "submit a proposal",
    "phrase_zh": "提交提案",
    "collocation_type": "verb + noun"
  },
  "scene_sentence": {
    "en": "He submitted a $2M budget proposal to the board while cradling it like a fragile infant.",
    "zh": "他像抱着易碎的婴儿一样向董事会提交了一份200万美元的预算提案。",
    "mood": "荒诞 / 反差 / 黑色幽默"
  },
  "image_prompt": "Surreal comic, flat colors: a nervous man in a suit tenderly cradling a massive proposal document like a baby in a cold corporate boardroom. Bold flat colors, exaggerated tender expression, absurd juxtaposition.",
  "derivatives": [
    { "word": "submission", "pos": "n.", "meaning_zh": "提交物；服从" },
    { "word": "submissive", "pos": "adj.", "meaning_zh": "服从的；顺从的" }
  ]
}
"""


def build_single_user_prompt(word: str, theme_hint: str = "") -> str:
    """构建单点深耕的用户提示词。"""
    theme_line = (
        f"\nTHEME HINT (optional): {theme_hint}"
        if theme_hint
        else "\nTHEME: Choose any TOEIC business context that fits the word."
    )
    return f"""Please generate the "one word, one image, one hook" memorization card for the following TOEIC word.

TARGET WORD: {word}
{theme_line}

Output ONLY the JSON object. No markdown, no explanation, no prefix text."""


def _extract_json(content: str) -> dict:
    """从 LLM 响应中稳健地提取 JSON 对象。
    处理：```json ``` 包裹、带前缀说明文字、纯 JSON、多余尾随文字。
    """
    if not content or not content.strip():
        raise HTTPException(500, "LLM 返回空内容，无法解析 JSON")
    text = content.strip()
    # 1) 剥离 ```json ... ``` 或 ``` ... ``` 包裹
    if text.startswith("```"):
        lines = text.split("\n")
        # 去掉首行 ```
        lines = lines[1:]
        # 如果第二行是 json/lang 标识，去掉
        if lines and lines[0].strip().lower() in ("json", "javascript", ""):
            lines = lines[1:]
        # 去掉末尾 ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # 2) 提取第一个 { 到最后一个 } 之间的内容
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise HTTPException(
            500,
            f"LLM 响应未找到有效 JSON 对象。响应前 200 字符: {content[:200]}",
        )
    json_str = text[first:last + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise HTTPException(
            500,
            f"LLM 响应 JSON 解析失败: {e}。响应前 200 字符: {content[:200]}",
        )


async def call_deepseek_single(word: str, theme_hint: str = ""):
    """调用 DeepSeek 生成单点深耕记忆卡片（词伙 + 场景句 + 图描述 + 派生词）。"""
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "请先设置 DEEPSEEK_API_KEY 环境变量")

    user_prompt = build_single_user_prompt(word, theme_hint)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SINGLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 2048,
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

    content = data["choices"][0]["message"]["content"]
    result = _extract_json(content)

    # 容错：保证关键字段存在
    result.setdefault("word", word)
    result.setdefault("collocation", {"phrase_en": "", "phrase_zh": "", "collocation_type": ""})
    result.setdefault("scene_sentence", {"en": "", "zh": "", "mood": ""})
    result.setdefault("image_prompt", "")
    result.setdefault("derivatives", [])
    return result, data.get("usage", {})


# ========================================================================
# 单点深耕图片生成
# ========================================================================

async def generate_single_image(prompt: str, model: str, gen_id: str) -> dict:
    """为单点深耕生成 1 张图片，存盘并返回 dict(url, error)。"""
    if not prompt:
        return {"url": None, "error": "无 image_prompt"}
    try:
        img_bytes = await call_image_generation(prompt, model)
    except HTTPException as e:
        return {"url": None, "error": e.detail}
    except Exception as e:
        return {"url": None, "error": f"图片生成失败: {e}"}
    if not img_bytes:
        return {"url": None, "error": "图片生成返回空数据"}

    file_name = f"{gen_id}_single.png"
    (IMAGES_DIR / file_name).write_bytes(img_bytes)
    return {"url": f"/images/{file_name}", "error": None}


# ========================================================================
# LLM 双模型兜底调用（先试廉价模型，失败降级到 DeepSeek）
# ========================================================================

async def _call_llm_with_fallback(
    messages: list[dict],
    temperature: float = 0.2,
    max_tokens: int = 2048,
    response_format: dict | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """调用 LLM，先试廉价模型，失败时降级到 DeepSeek。
    返回解析后的 data dict；两者都失败时返回 None。"""
    payload: dict = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    # 1) 尝试廉价模型（如智谱 GLM-4.7-Flash）
    if CHEAP_LLM_API_KEY:
        payload["model"] = CHEAP_LLM_MODEL
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{CHEAP_LLM_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {CHEAP_LLM_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                # 429 限流直接降级，不重试
                if resp.status_code == 429:
                    pass
                else:
                    resp.raise_for_status()
                    return resp.json()
        except Exception:
            pass

    # 2) 降级到 DeepSeek
    if not DEEPSEEK_API_KEY:
        return None
    payload["model"] = DEEPSEEK_MODEL
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


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
    """调用 LLM 批量补充单词的词性和中文释义（先试廉价模型，失败降级到 DeepSeek）。"""
    if not words:
        return {"results": []}
    if not DEEPSEEK_API_KEY and not CHEAP_LLM_API_KEY:
        return {"results": [], "skipped": True, "reason": "no_api_key"}

    user_prompt = _build_enrich_prompt(words)
    messages = [
        {"role": "system", "content": WORD_ENRICH_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    data = await _call_llm_with_fallback(
        messages=messages,
        temperature=0.2,
        max_tokens=2048,
        response_format={"type": "json_object"},
        timeout=30.0,
    )

    if data is None:
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


# ========================================================================
# 场景聚汇：自动检测
# ========================================================================

SCENE_DETECT_SYSTEM_PROMPT = """You are a TOEIC vocabulary curator.

You will receive:
1) A list of existing scenes (scene_id + name + name_zh + description)
2) A list of words that need to be assigned to scenes

For each word, decide which scene it best fits. Use the existing scenes if possible; if a word does not fit any existing scene well, propose a NEW scene.

OUTPUT JSON ONLY:
{
  "scene_assignments": [
    {"word": "...", "scene_id": 3, "confidence": 0.85, "low_confidence": false},
    ...
  ],
  "new_scenes_suggested": [
    {
      "name": "Customer Service",
      "name_zh": "客户服务",
      "description": "客户咨询、投诉、售后支持",
      "suggested_words": ["refund", "complain", "inquiry", ...],
      "confidence": 0.8
    },
    ...
  ]
}

RULES:
1. Each existing-scene assignment confidence ∈ [0,1]. If < 0.5, set low_confidence=true.
2. Don't force-fit. If a group of words clearly forms a new TOEIC business scene not in the existing list, propose a new scene in new_scenes_suggested (max 3 new scenes).
3. scene_id in scene_assignments MUST be one of the existing scene IDs. New scene suggestions go ONLY into new_scenes_suggested.
4. Every word MUST appear exactly once in scene_assignments.
5. Output only the JSON object."""


def _build_scene_detect_user_prompt(words: list[str], existing_scenes: list[dict]) -> str:
    """构造场景检测用户 prompt。"""
    lines = [f"EXISTING_SCENES ({len(existing_scenes)}):"]
    for s in existing_scenes:
        name = s.get('name_en') or s.get('name') or ''
        lines.append(f"- scene_id={s['id']} | name={name} | name_zh={s.get('name_zh','')} | description={s.get('description','')}")
    lines.append("")
    lines.append(f"WORDS_TO_ASSIGN ({len(words)}):")
    for w in words:
        lines.append(f"- {w}")
    lines.append("")
    lines.append("Output only the JSON object as described.")
    return "\n".join(lines)


# 无已有场景时的"纯分组"模式 prompt
SCENE_DETECT_GROUPING_SYSTEM_PROMPT = """You are a TOEIC vocabulary curator.

Group the given words into 3-6 TOEIC business scenes. There are NO existing scenes.

Output ONLY this JSON:
{"new_scenes_suggested": [{"name": "HR", "name_zh": "人力资源", "description": "招聘薪酬", "suggested_words": ["word1","word2"], "confidence": 0.9}]}

Rules:
1. Each word goes into exactly ONE scene.
2. Use TOEIC domains: HR, Finance, Logistics, Meetings, Contracts, Marketing etc.
3. Keep description short (Chinese, one line).
4. Do NOT include scene_assignments field.
5. Output only JSON, no other text."""


def _build_scene_detect_grouping_user_prompt(words: list[str]) -> str:
    """构造纯分组模式的用户 prompt。"""
    lines = [f"WORDS_TO_GROUP ({len(words)}):"]
    for w in words:
        lines.append(f"- {w}")
    lines.append("")
    lines.append("There are NO existing scenes. Propose 3-8 new scenes covering all words above.")
    lines.append("Output only the JSON object with scene_assignments=[] and new_scenes_suggested=[...].")
    return "\n".join(lines)


async def call_deepseek_scene_detect(words: list[str], existing_scenes: list[dict]) -> dict:
    """调用 DeepSeek 进行场景检测。
    返回 {"scene_assignments": [...], "new_scenes_suggested": [...]}。
    """
    if not words:
        return {"scene_assignments": [], "new_scenes_suggested": []}
    if not DEEPSEEK_API_KEY and not CHEAP_LLM_API_KEY:
        raise HTTPException(500, "未配置 DEEPSEEK_API_KEY 或 CHEAP_LLM_API_KEY")

    # 无已有场景时，切换为"纯分组"模式：让 LLM 直接对所有词做场景分组
    has_existing = len(existing_scenes) > 0
    if has_existing:
        system_prompt = SCENE_DETECT_SYSTEM_PROMPT
        user_prompt = _build_scene_detect_user_prompt(words, existing_scenes)
    else:
        system_prompt = SCENE_DETECT_GROUPING_SYSTEM_PROMPT
        user_prompt = _build_scene_detect_grouping_user_prompt(words)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    data = await _call_llm_with_fallback(
        messages=messages,
        temperature=0.2,
        max_tokens=8192,
        response_format={"type": "json_object"},
        timeout=90.0,
    )
    if data is None:
        raise HTTPException(500, "LLM 调用失败（双模型均无响应）")

    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        # LLM 返回空内容：返回空结果而非抛异常，让前端正常显示
        return {
            "scene_assignments": [],
            "new_scenes_suggested": [],
            "warning": "LLM 返回空内容，请稍后重试或检查 API Key",
        }
    try:
        parsed = _extract_json(content)
    except HTTPException:
        return {
            "scene_assignments": [],
            "new_scenes_suggested": [],
            "warning": f"LLM 响应解析失败，请稍后重试。响应前 200 字符: {content[:200]}",
        }

    # 容错与清洗
    assignments = []
    existing_ids = {s["id"] for s in existing_scenes}
    for a in parsed.get("scene_assignments", []):
        w = str(a.get("word", "")).strip().lower()
        sid = a.get("scene_id")
        conf = float(a.get("confidence", 0.0))
        low = bool(a.get("low_confidence", conf < 0.5))
        if not w or sid is None or sid not in existing_ids:
            continue
        assignments.append({
            "word": w,
            "scene_id": int(sid),
            "confidence": conf,
            "low_confidence": low,
        })

    new_scenes = []
    for ns in parsed.get("new_scenes_suggested", [])[:5]:
        name = str(ns.get("name", "")).strip()[:60]
        name_zh = str(ns.get("name_zh", "")).strip()[:60]
        desc = str(ns.get("description", "")).strip()[:300]
        suggested = [str(w).strip().lower() for w in ns.get("suggested_words", []) if str(w).strip()][:50]
        conf = float(ns.get("confidence", 0.5))
        if not name:
            continue
        new_scenes.append({
            "name": name,
            "name_zh": name_zh,
            "description": desc,
            "suggested_words": suggested,
            "confidence": conf,
        })

    return {"scene_assignments": assignments, "new_scenes_suggested": new_scenes}
