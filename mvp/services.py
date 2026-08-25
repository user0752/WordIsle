"""
TOEIC MVP 外部服务
==================
DeepSeek AI 生成、百炼 TTS 语音合成、百炼文生图。
"""

import asyncio
import base64
import functools
import json
import logging
import re
import sys
import time

import dashscope
import httpx
from dashscope.audio.http_tts import HttpSpeechSynthesizer
from fastapi import HTTPException

from config import *
from db import record_model_usage

# 统一日志：如实记录每次模型调用与失败原因（用户侧只显示兜底话术，后台看这里定位问题）
logger = logging.getLogger("toeic.services")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

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
    "call_word_extraction",
    "call_word_phonetic",
    "call_morpheme_detect",
    "call_morpheme_seed",
    "call_deepseek_scene_detect",
    "call_deepseek_scene_collocations",
    "call_video_script",
    "call_video_generation",
    "mux_video_with_audio",
    "_get_video_model_config",
    "get_route_llm",
    "route_llm_candidates",
    "resolve_llm_model",
    "_chat_completion",
]

# ========================================================================
# 语境赛道（track）：同一套记忆钩子策略，不同的语境风味
#   general —— 通用语境（默认，考试/职场/生活场景）
#   tech    —— 程序员语境（commit / PR review / 技术文档 / 终端报错 / standup）
# 注入方式：user prompt 中的 CONTEXT TRACK 指令覆盖 system prompt 的商务语境设定。
# ========================================================================

TRACKS = {
    "general": {
        "value": "general",
        "label": "通用语境",
        "desc": "考试 / 职场 / 生活场景（默认）",
    },
    "tech": {
        "value": "tech",
        "label": "程序员语境",
        "desc": "commit / PR review / 技术文档 / 终端报错 / standup",
    },
}
DEFAULT_TRACK = "general"

TECH_TRACK_INSTRUCTION = """CONTEXT TRACK: TECH / PROGRAMMER WORLD — this OVERRIDES any business-office context in the system prompt.
All collocations, sentences, and image prompts MUST live in a software developer's world:
- Scenes: code editor, terminal, git history, CI pipeline, server room, tech office, hackathon, code review, on-call incident, tech conference, whiteboard architecture talk.
- Roles: developer, tech lead, SRE, code reviewer, new intern, product manager, QA engineer.
- Artifacts: commit messages, pull request reviews, stack traces, changelogs, README, API docs, error logs, standup notes, architecture diagrams.
- Sentences should read like something a developer actually says, writes in a commit/PR/doc, or sees in a terminal/log (e.g. "fix: refactor the deprecated invoice parser", "LGTM, but this will brick the legacy pipeline", "PANIC: deadline exceeded").
- Keep ALL memory-hook strategies (absurd contrast, exaggeration, visual metaphor, side-by-side comparison) — just set them in programmer contexts, and make tech artifacts (screens, terminals, servers, keyboards, sticky notes on monitors) the visual carriers.
- Chinese meanings may briefly note the word's tech flavor (e.g. deprecated 在技术语境中常指「已弃用的 API」)."""


def _track_instruction(track: str) -> str:
    """把赛道 value 转为注入 user prompt 的语境指令；general 返回通用指令。"""
    if track == "tech":
        return TECH_TRACK_INSTRUCTION
    return ("CONTEXT TRACK: GENERAL — pick any vivid everyday / exam / workplace scenario that "
            "makes the target words most memorable.")


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


def build_user_prompt(words: list[str], panel_count: int = 4, theme_hint: str = "", track: str = DEFAULT_TRACK):
    """构建 DeepSeek 用户提示词（旧版微电影风格，向后兼容）。track 注入语境赛道指令。"""
    words_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    theme_line = (
        f"\nTHEME HINT (optional, you may follow or override): {theme_hint}"
        if theme_hint
        else "\nTHEME: Choose any scenario matching the CONTEXT TRACK below, with a clear arc. Be creative."
    )
    return f"""Please write a business English CINEMATIC STORY split into {panel_count} visual panels.

TARGET WORDS ({len(words)} total):
{words_list}

PANEL COUNT: {panel_count} (must be exactly {panel_count} panels)
{_track_instruction(track)}
{theme_line}

CONSTRAINTS:
- Each panel's English sentence: 12-25 words, containing 2-4 target words.
- Distribute ALL {len(words)} target words across the {panel_count} panels as evenly as possible.
- Story must have a clear arc with setup/development/climax/resolution roles.
- image_prompt: cinematic storyboard style, 16:9, film grain, visual continuity across panels.
- All target words must appear naturally in business context.

Output only the JSON object."""


# ========================================================================
# 批量编译新风格：荒诞三连弹（absurd）
# ========================================================================

BATCH_ABSURD_SYSTEM_PROMPT = """You are a TOEIC vocabulary curator specializing in ABSURD MEMORABLE CARDS.

CORE IDEA: Pack target words into 3 INDEPENDENT absurd scenes. Each scene is self-contained, loosely linked by the same theme or characters. Absurdity = memorable.

RULES:
1. Exactly 3 panels. Each panel = one independent absurd scene.
2. Each English sentence: 8-15 words, VERY short and punchy.
3. Pack 3-5 target words per panel via business collocations (use common inflections if needed).
4. Each panel's image_prompt: surreal comic, absurd juxtaposition, bold flat colors, weird objects. Any English text rendered inside the image must be lowercase. 16:9.
5. Each panel has 2-4 collocations (business chunks containing target words).
6. No scene_role, no ending_moral. Story_title is just a label for the list.
7. Image must be ABSURD: literal meaning + business meaning forced into one frame.
8. Output ONLY valid JSON.

JSON STRUCTURE:
{
  "story_title": "English title (3-6 words)",
  "theme": "Chinese theme (e.g. 财务危机)",
  "story_synopsis": "Chinese one-line summary",
  "panels": [
    {
      "scene_index": 1,
      "sentence_en": "Short absurd English sentence with 3-5 target words.",
      "sentence_zh": "Chinese translation.",
      "target_words_in_scene": ["word1","word2"],
      "word_notes": {"word1": "中文商务释义"},
      "collocations": ["business collocation 1","business collocation 2"],
      "image_prompt": "Surreal comic: [absurd scene]. Bold flat colors. Weird objects. 16:9."
    }
  ],
  "included_words": ["word1","word2"],
  "missing_words": [],
  "polysemy_notes": {}
}
"""


def build_batch_absurd_user_prompt(words: list[str], theme_hint: str = "", art_style: str = "", track: str = DEFAULT_TRACK):
    """构建荒诞三连弹用户提示词。track 注入语境赛道指令。"""
    words_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    theme_line = (
        f"\nTHEME HINT (optional): {theme_hint}"
        if theme_hint
        else "\nTHEME: Choose any scenario fitting the CONTEXT TRACK below."
    )
    style_line = f"\nART STYLE: {_art_style_instruction(art_style)}" if art_style else ""
    return f"""Create 3 ABSURD MEMORABLE CARDS for the following words.

TARGET WORDS ({len(words)} total):
{words_list}
{_track_instruction(track)}
{theme_line}
{style_line}

CONSTRAINTS:
- These words may be totally unrelated (low relatedness). Your job is creative forced LINKING — weave them together with surprising, memorable connections. The more unexpected the linkage, the better the memory hook.
- Exactly 3 panels, each an independent absurd scene.
- Each sentence: 8-15 words, containing 3-5 target words.
- Distribute ALL {len(words)} words across the 3 panels.
- Image style: surreal comic, absurd juxtaposition, bold flat colors.
- Each panel must have 2-4 business collocations.

Output only the JSON object."""


# ========================================================================
# 批量编译新风格：冲突连环（conflict）
# ========================================================================

BATCH_CONFLICT_SYSTEM_PROMPT = """You are a TOEIC vocabulary curator specializing in CONFLICT COMIC STRIPS.

CORE IDEA: Two opposing characters × 3 rounds of conflict escalation. Conflict → emotion → amygdala engagement → stronger memory. Aligned with TOEIC business negotiation scenarios.

RULES:
1. Exactly 3 panels, structured as rounds:
   - Panel 1 (round_1): A方出招 (Party A makes a move)
   - Panel 2 (round_2): B方反击 (Party B counters)
   - Panel 3 (round_3): 荒诞结局 (Absurd resolution)
2. Each English sentence: 10-18 words (slightly longer to convey conflict).
3. Pack 3-5 target words per panel via business collocations.
4. Each panel's image_prompt: comic strip style, exaggerated character expressions, focus on two-person interaction. Any English text rendered inside the image must be lowercase. 16:9.
5. Each panel has a round_label: "A方出招" / "B方反击" / "荒诞结局".
6. Each panel has 2-4 collocations.
7. Choose a conflict type: buyer vs seller / boss vs employee / vendor vs procurement / HQ vs branch.
8. No scene_role, no ending_moral. Story_title is just a label.
9. Output ONLY valid JSON.

JSON STRUCTURE:
{
  "story_title": "English title (3-6 words)",
  "theme": "Chinese theme (e.g. 采购谈判)",
  "story_synopsis": "Chinese one-line summary of the conflict",
  "panels": [
    {
      "scene_index": 1,
      "round_label": "A方出招",
      "sentence_en": "English sentence showing Party A's move with 3-5 target words.",
      "sentence_zh": "Chinese translation.",
      "target_words_in_scene": ["word1","word2"],
      "word_notes": {"word1": "中文商务释义"},
      "collocations": ["business collocation 1"],
      "image_prompt": "Comic strip: [two characters interacting, exaggerated expressions]. 16:9."
    }
  ],
  "included_words": ["word1","word2"],
  "missing_words": [],
  "polysemy_notes": {}
}
"""


def build_batch_conflict_user_prompt(words: list[str], theme_hint: str = "", art_style: str = "", track: str = DEFAULT_TRACK):
    """构建冲突连环用户提示词。track 注入语境赛道指令，tech 时冲突双方为技术角色。"""
    words_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    theme_line = (
        f"\nCONFLICT TYPE HINT (optional): {theme_hint}"
        if theme_hint
        else "\nCONFLICT TYPE: Choose one fitting the CONTEXT TRACK (e.g. developer vs tech lead / code reviewer vs author / PM vs engineer)."
    )
    style_line = f"\nART STYLE: {_art_style_instruction(art_style)}" if art_style else ""
    return f"""Create a 3-ROUND CONFLICT COMIC STRIP for the following words.

TARGET WORDS ({len(words)} total):
{words_list}
{_track_instruction(track)}
{theme_line}
{style_line}

CONSTRAINTS:
- These words may be totally unrelated (low relatedness). Your job is creative forced LINKING — stage a conflict that naturally (or wittily) connects them all. The more unexpected the connection, the better the memory hook.
- Exactly 3 panels: round_1 (A方出招) → round_2 (B方反击) → round_3 (荒诞结局).
- Each sentence: 10-18 words, containing 3-5 target words.
- Distribute ALL {len(words)} words across the 3 panels.
- Image style: comic strip, exaggerated expressions, two-character interaction.
- Each panel must have round_label and 2-4 business collocations.

Output only the JSON object."""


# ========================================================================
# 场景编译专用 Prompt（高关联词：自然连贯、完整覆盖、复用场景词伙）
# ========================================================================

SCENE_SYSTEM_PROMPT = """You are a TOEIC vocabulary curator specializing in SCENE-BASED story comics.

CORE DISTINCTION: These words belong to ONE business scene (high relatedness). Your job is to weave them into a NATURAL, coherent mini-story that covers ALL of them — NOT to force absurdity. The scene itself is the memory cue.

RULES:
1. Exactly 3 panels, one continuous mini-story (start → middle → resolution).
2. Cover ALL given scene words across the panels. Reuse the provided scene collocations verbatim where possible, so the comic matches the scene's vocabulary. Do NOT omit words.
3. Each panel: 1 English sentence (10-18 words) containing 2-4 scene words, via natural business collocations.
4. Each panel has collocations (business chunks) and word_notes (Chinese business definitions).
5. Each panel's image_prompt: natural, coherent cartoon scene matching the scene theme; consistent characters across panels. Any English text rendered inside the image must be lowercase. 16:9.
6. Polysemous scene words: show the business meaning in context (no need to force the everyday meaning).
7. Output ONLY valid JSON.

JSON STRUCTURE:
{
  "story_title": "English title (3-6 words)",
  "theme": "Chinese theme (e.g. 采购谈判)",
  "story_synopsis": "Chinese one-line summary",
  "panels": [
    {
      "scene_index": 1,
      "sentence_en": "Natural English sentence covering 2-4 scene words.",
      "sentence_zh": "Chinese translation.",
      "target_words_in_scene": ["word1","word2"],
      "word_notes": {"word1": "中文商务释义"},
      "collocations": ["business collocation 1"],
      "image_prompt": "Coherent cartoon scene, consistent characters. 16:9."
    }
  ],
  "included_words": ["word1","word2"],
  "missing_words": [],
  "polysemy_notes": {}
}
"""


def build_scene_user_prompt(words: list[str], theme_hint: str = "", collocations: list = None, art_style: str = "", track: str = DEFAULT_TRACK):
    """构建场景编译用户提示词，把已生成的场景词伙作为词伙约束喂入。track 注入语境赛道指令。"""
    words_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    theme_line = f"\nTHEME: {theme_hint}" if theme_hint else "\nTHEME: Choose the scene's scenario fitting the CONTEXT TRACK below."
    col_line = ""
    if collocations:
        col_list = "\n".join(f"  - {c}" for c in collocations)
        col_line = f"""
REUSE THESE SCENE COLLOCATIONS verbatim where possible (they are the scene's vocabulary, keep the comic consistent with them):
{col_list}"""
    style_line = f"\nART STYLE: {_art_style_instruction(art_style)}" if art_style else ""
    return f"""Create a NATURAL, COHERENT 3-panel story comic that covers ALL the following scene words.

SCENE WORDS ({len(words)} total):
{words_list}
{_track_instruction(track)}
{theme_line}
{col_line}
{style_line}

CONSTRAINTS:
- Exactly 3 panels, one continuous mini-story covering ALL {len(words)} words.
- Each sentence: 10-18 words, containing 2-4 scene words via natural business collocations.
- Story must feel natural for this scene, not absurd. No word may be silently dropped.

Output only the JSON object."""


# ========================================================================
# DeepSeek AI 生成
# ========================================================================

async def call_deepseek(words: list[str], panel_count: int = 4, theme_hint: str = "", style: str = "", collocations: list = None, art_style: str = "", track: str = DEFAULT_TRACK):
    """调用 LLM（批量编译路由，可切换模型）生成剧情连环画。
    选定模型调用失败（如限流）时自动降级到默认主模型。
    style: '' 或 'legacy' 走旧版微电影；'absurd' 荒诞三连弹；'conflict' 冲突连环；'scene' 场景编译。
    collocations: 场景编译时传入的已有场景词伙（可选）。
    art_style: 可选画风（comic/realistic/3d/watercolor/pixel），空表示不指定。
    track: 语境赛道（general/tech），tech 时所有语境切换为程序员世界。
    """

    # 根据风格分派 prompt
    if style == "scene":
        system_prompt = SCENE_SYSTEM_PROMPT
        user_prompt = build_scene_user_prompt(words, theme_hint, collocations, art_style, track)
        effective_panel_count = 3
    elif style == "absurd":
        system_prompt = BATCH_ABSURD_SYSTEM_PROMPT
        user_prompt = build_batch_absurd_user_prompt(words, theme_hint, art_style, track)
        effective_panel_count = 3
    elif style == "conflict":
        system_prompt = BATCH_CONFLICT_SYSTEM_PROMPT
        user_prompt = build_batch_conflict_user_prompt(words, theme_hint, art_style, track)
        effective_panel_count = 3
    else:
        # 旧版微电影（向后兼容）
        system_prompt = SYSTEM_PROMPT
        user_prompt = build_user_prompt(words, panel_count, theme_hint, track)
        effective_panel_count = panel_count

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
    }

    last_err = None
    actual_model = None
    for cfg in route_llm_candidates("batch"):
        if not cfg.get("api_key"):
            continue
        logger.info("LLM 批量编译调用 model=%s(%s) style=%s panel_count=%s", cfg["model"], cfg["value"], style, panel_count)
        try:
            data = await _chat_completion(cfg["base_url"], cfg["api_key"], cfg["model"], payload, timeout=120.0, detail="批量编译")
            actual_model = cfg["model"]
            break
        except (httpx.HTTPError, KeyError) as e:
            logger.warning("LLM 批量编译调用失败 model=%s error=%r，尝试下一候选模型", cfg["model"], e)
            last_err = e
            continue
    else:
        if last_err is None:
            logger.error("LLM 批量编译失败：未找到已配置 API Key 的可用模型")
            raise HTTPException(500, "未找到已配置 API Key 的可用模型，请检查环境变量或更换模型")
        logger.error("LLM 批量编译失败（已尝试全部候选模型）: %r", last_err)
        raise HTTPException(500, f"LLM 调用失败（已尝试全部候选模型）: {last_err}")

    content = data["choices"][0]["message"]["content"]
    result = _extract_json(content)
    result["_llm_model"] = actual_model

    # 兼容：确保 panels 数量与 effective_panel_count 一致
    panels = result.get("panels", [])
    if len(panels) != effective_panel_count and panels:
        result["panels"] = panels[:effective_panel_count] if len(panels) > effective_panel_count else panels

    return result, data.get("usage", {})


# ========================================================================
# 单点深耕 Prompt & 生成
# ========================================================================

SINGLE_SYSTEM_PROMPT = """You are a TOEIC Business English coach specialized in the "one word, one image, one hook" memorization technique.

CORE IDEA: Given ONE English word, produce a single vivid memory-hook CARD. The image and the scene sentence together become the recall trigger — when the user sees the picture, they instantly remember the word.

## MEMORY-HOOK STRATEGY (classify the word, then apply ONLY the matching strategy)
First decide which type the word falls into, set hook_type accordingly, and follow ONLY that strategy:

A. POLYSEMY word — has a common everyday meaning AND a distinct business meaning (e.g. tender, address, submit, firm, charge) → hook_type "双义碰撞"
   - Collide the common meaning and the business meaning in the SAME frame to create absurd contrast.

B. SINGLE-MEANING / CONCRETE word — mostly one meaning, a concrete thing or action (e.g. invoice, receipt, warehouse, headset) → hook_type "夸张场景"
   - Build ONE exaggerated, vivid mini-scene around the word; the bigger / weirder / more emotional it is, the more memorable.

C. ABSTRACT business word — an abstract concept (e.g. strategy, compliant, leverage, revenue) → hook_type "具象比喻"
   - Render the abstract concept as a concrete, visual metaphor in the image.

D. EASY-TO-CONFUSE word — part of a commonly confused pair (e.g. affect/effect, principal/principle, assure/ensure) → hook_type "对比记忆"
   - Show the word AND its confused partner side by side with a clear visual difference.

If the word fits multiple types, choose the strategy that produces the clearest single visual.

## TASK
Given one English target word, output:
1. ONE high-frequency TOEIC collocation (e.g. submit → submit a proposal).
2. ONE vivid scene sentence (English + Chinese) that matches the chosen strategy, MUST contain the target word.
3. ONE image_prompt that makes the WORD ITSELF the visual focus, following the chosen strategy.
4. The word's derivative family (noun/verb/adjective/adverb forms, with Chinese meanings).

## IMAGE REQUIREMENT
- image_prompt MUST follow the chosen strategy and make the word the visual focus.
- WORD-IN-IMAGE (critical): the target word ITSELF must appear inside the image as part of the MAIN SUBJECT — either as large bold lowercase typography integrated into the scene (painted on the object, looming in the sky, formed by objects), or as a visual pun where the word's letters physically build / label the subject (e.g. the word "broker" with a visibly broken "k"). Never render it as a tiny caption or a corner label.
- image_prompt MUST be in English, 1-3 sentences, following the ART STYLE specified in the user prompt (default: surreal comic / flat illustration).
- The image is the recall cue: seeing the picture reminds the user of the word.
- Any English word text rendered INSIDE the image (e.g. a label, sign, caption, or the target word itself) MUST be written in lowercase letters.

## RULES
1. scene_sentence.en: 10-20 words, contains the target word, business context, matches the strategy tone.
2. scene_sentence.mood: 2-3 Chinese tags describing the tone (e.g. 荒诞 / 反差 / 夸张 / 对比).
3. derivatives: 2-4 items; if no common derivatives exist, return an empty array.
4. collocation_type: grammatical pattern (e.g. verb + noun, adj + noun, noun + noun).
5. Output ONLY a valid JSON object. No markdown, no extra text.

## JSON STRUCTURE
{
  "word": "tender",
  "meaning_zh": "投标；投标书；温柔的",
  "hook_type": "双义碰撞",
  "collocation": {
    "phrase_en": "submit a tender",
    "phrase_zh": "提交投标书",
    "collocation_type": "verb + noun"
  },
  "scene_sentence": {
    "en": "He submitted a $2M tender while cradling it like a fragile infant.",
    "zh": "他像抱着易碎的婴儿一样提交了一份200万美元的投标书。",
    "mood": "荒诞 / 反差 / 黑色幽默"
  },
  "image_prompt": "Surreal comic, flat colors: a nervous man in a suit tenderly cradling a massive proposal document like a baby in a cold corporate boardroom, the word 'tender' painted in huge bold lowercase letters across the document cover as the visual focus. Bold flat colors, exaggerated tender expression, absurd juxtaposition.",
  "derivatives": [
    { "word": "submission", "pos": "n.", "meaning_zh": "提交物；服从" },
    { "word": "submissive", "pos": "adj.", "meaning_zh": "服从的；顺从的" }
  ]
}
"""


# 单点深耕可配置画风（value → 英文风格指令，传入 LLM 提示词）
# 前端下拉与后端共用此映射；默认漫画/扁平
ART_STYLES = {
    "comic": "Surreal comic / flat illustration style (bold flat colors, exaggerated shapes)",
    "realistic": "Photorealistic style, natural lighting and textures",
    "3d": "3D render style, playful Pixar-like, soft rounded shapes",
    "watercolor": "Watercolor illustration style, soft color washes",
    "pixel": "Pixel art style, retro 8-bit, chunky pixels",
}
DEFAULT_ART_STYLE = "comic"
AUTO_ART_STYLE_INSTRUCTION = ("AUTO — 根据单词的语义与记忆策略，从「漫画扁平风 / 写实 / 3D / 水彩 / 像素」"
                              "中自动选择最能强化记忆的画风，并在每个 image_prompt 的开头明确写出所选画风。")


def _art_style_instruction(art_style: str) -> str:
    """把画风 value 转成传给 LLM 的英文风格指令；auto 时让 LLM 自行选择。"""
    if art_style == "auto":
        return AUTO_ART_STYLE_INSTRUCTION
    return ART_STYLES.get(art_style, ART_STYLES[DEFAULT_ART_STYLE])


def build_single_user_prompt(word: str, theme_hint: str = "", art_style: str = DEFAULT_ART_STYLE, track: str = DEFAULT_TRACK) -> str:
    """构建单点深耕的用户提示词。track 注入语境赛道指令，tech 时覆盖商务语境。"""
    theme_line = (
        f"\nTHEME HINT (optional): {theme_hint}"
        if theme_hint
        else "\nTHEME: Choose any context that fits the word and the CONTEXT TRACK below."
    )
    if art_style == "auto":
        style_instruction = ("AUTO — 根据该单词的语义与记忆策略，从「漫画扁平风 / 写实 / 3D / 水彩 / 像素」"
                             "中自动选择最能强化记忆的画风，并在 image_prompt 的开头明确写出所选画风。")
    else:
        style_instruction = ART_STYLES.get(art_style, ART_STYLES[DEFAULT_ART_STYLE])
    return f"""Please generate the "one word, one image, one hook" memorization card for the following word.

TARGET WORD: {word}
{_track_instruction(track)}
ART STYLE: {style_instruction}
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


async def call_deepseek_single(word: str, theme_hint: str = "", art_style: str = DEFAULT_ART_STYLE, track: str = DEFAULT_TRACK):
    """调用 LLM（单点深耕路由，可切换模型）生成单点深耕记忆卡片（词伙 + 场景句 + 图描述 + 派生词）。
    选定模型调用失败（如限流）时自动降级到默认主模型；LLM 偶发返回残缺/非 JSON 响应时自动重试一次。
    track: 语境赛道（general/tech），tech 时语境切换为程序员世界。"""
    user_prompt = build_single_user_prompt(word, theme_hint, art_style, track)
    payload = {
        "messages": [
            {"role": "system", "content": SINGLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    last_err = None
    result = None
    for cfg in route_llm_candidates("single"):
        if not cfg.get("api_key"):
            continue
        logger.info("LLM 单点深耕调用 model=%s(%s) word=%s art_style=%s", cfg["model"], cfg["value"], word, art_style)
        for attempt in range(2):
            try:
                data = await _chat_completion(cfg["base_url"], cfg["api_key"], cfg["model"], payload, timeout=120.0, detail="单点深耕")
                content = data["choices"][0]["message"]["content"]
                result = _extract_json(content)
                break  # 本模型成功
            except HTTPException as e:
                # 仅 JSON 解析类错误值得重试；API key 缺失等直接抛出
                if "JSON" not in str(e.detail) and "有效 JSON" not in str(e.detail):
                    raise
                last_err = e
                logger.warning("LLM 单点深耕返回无效 JSON（第 %s 次）model=%s，重试中", attempt + 1, cfg["model"])
                await asyncio.sleep(1.0)
                continue
            except (httpx.HTTPError, KeyError) as e:
                # 模型调用失败（限流/网络等）：记录后尝试下一个候选模型
                logger.warning("LLM 单点深耕调用失败 model=%s error=%r，尝试下一候选模型", cfg["model"], e)
                last_err = e
                break
        if result is not None:
            result["_llm_model"] = cfg["model"]
            break
    if result is None:
        if last_err is None:
            logger.error("LLM 单点深耕失败：未找到已配置 API Key 的可用模型")
            raise HTTPException(500, "未找到已配置 API Key 的可用模型，请检查环境变量或更换模型")
        if isinstance(last_err, HTTPException):
            raise last_err
        logger.error("LLM 单点深耕失败（已尝试全部候选模型）: %r", last_err)
        raise HTTPException(500, f"LLM 调用失败（已尝试全部候选模型）: {last_err}")

    # 容错：保证关键字段存在
    result.setdefault("word", word)
    result.setdefault("hook_type", "")
    result.setdefault("collocation", {"phrase_en": "", "phrase_zh": "", "collocation_type": ""})
    result.setdefault("scene_sentence", {"en": "", "zh": "", "mood": ""})
    result.setdefault("image_prompt", "")
    result.setdefault("derivatives", [])
    return result, data.get("usage", {})


# ========================================================================
# 单点深耕图片生成
# ========================================================================

async def generate_single_image(prompt: str, model: str, gen_id: str, feature: str = "") -> dict:
    """为单点深耕生成 1 张图片，存盘并返回 dict(url, error)。
    feature: 编译功能名（如「单点深耕」），用于用量明细的「说明」字段。"""
    if not prompt:
        return {"url": None, "error": "无 image_prompt"}
    try:
        img_bytes = await call_image_generation(prompt, model, feature=feature)
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
# 百炼文生视频（视频编译）
# ========================================================================

VIDEO_SYSTEM_PROMPT = """You are a TOEIC Business English video director. Given a list of TOEIC words, write a short MEMORY MICROFILM in the form of a text-to-video prompt.

CORE IDEA: The user learns words by watching a short video (5-10s). The video must visually encode the words so that seeing it triggers recall. Weave ALL target words into ONE coherent cinematic scene showing their business meanings.

RULES:
1. The words may be totally unrelated — your job is a creative forced LINKING into a single memorable scene.
2. narration_en: 1-3 short English sentences (~15-25 words total) that naturally include as many target words as possible; spoken or shown as subtitles.
3. narration_zh: Chinese translation of the narration.
4. video_prompt: English text-to-video prompt (1-3 sentences) describing scene, camera motion, characters, and objects that visually depict the words' business meanings. Any English text rendered inside the video (signs, labels, captions) MUST be lowercase.
5. Output ONLY a valid JSON object. No markdown.

JSON STRUCTURE:
{
  "story_title": "English title (3-6 words)",
  "narration_en": "English narration sentences",
  "narration_zh": "Chinese translation",
  "video_prompt": "Cinematic text-to-video prompt with camera motion",
  "included_words": ["word1","word2"],
  "missing_words": []
}
"""


def build_video_user_prompt(words: list[str], theme_hint: str = "", art_style: str = "", track: str = DEFAULT_TRACK):
    """构建视频编译用户提示词。track 注入语境赛道指令。"""
    words_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    theme_line = f"\nTHEME HINT (optional): {theme_hint}" if theme_hint else ""
    style_line = f"\nART STYLE: {_art_style_instruction(art_style)}" if art_style else ""
    return f"""Write a short MEMORY MICROFILM video for the following words.

TARGET WORDS ({len(words)} total):
{words_list}
{_track_instruction(track)}
{theme_line}
{style_line}

CONSTRAINTS:
- These words may be totally unrelated (low relatedness). Creatively force-link them into ONE coherent cinematic scene.
- STRICTLY follow the ART STYLE above when describing the visual look in video_prompt (scene, characters, textures, lighting). If ART STYLE is AUTO, you choose the most memorization-effective style and state it explicitly at the start of video_prompt.
- narration_en: 1-3 short English sentences naturally containing as many target words as possible.
- video_prompt: English, 1-3 sentences; describe the scene, camera motion, and how the words' meanings are visually encoded. Lowercase for any in-video text.
- Cover as many words as possible; list any not covered in missing_words.

Output only the JSON object."""


async def call_video_script(words: list[str], theme_hint: str = "", art_style: str = "", track: str = DEFAULT_TRACK):
    """调用 LLM（视频脚本路由，可切换模型）生成视频脚本（旁白 + 视频提示词）。
    选定模型调用失败（如限流）时自动降级到默认主模型。track: 语境赛道（general/tech）。"""
    payload = {
        "messages": [
            {"role": "system", "content": VIDEO_SYSTEM_PROMPT},
            {"role": "user", "content": build_video_user_prompt(words, theme_hint, art_style, track)},
        ],
        "temperature": 0.8,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }
    last_err = None
    for cfg in route_llm_candidates("video"):
        if not cfg.get("api_key"):
            continue
        logger.info("LLM 视频脚本调用 model=%s(%s) words=%s", cfg["model"], cfg["value"], len(words))
        try:
            data = await _chat_completion(cfg["base_url"], cfg["api_key"], cfg["model"], payload, timeout=120.0, detail="视频脚本")
            break
        except (httpx.HTTPError, KeyError) as e:
            logger.warning("LLM 视频脚本调用失败 model=%s error=%r，尝试下一候选模型", cfg["model"], e)
            last_err = e
            continue
    else:
        if last_err is None:
            logger.error("LLM 视频脚本失败：未找到已配置 API Key 的可用模型")
            raise HTTPException(500, "未找到已配置 API Key 的可用模型，请检查环境变量或更换模型")
        logger.error("LLM 视频脚本失败（已尝试全部候选模型）: %r", last_err)
        raise HTTPException(500, f"LLM 视频脚本调用失败（已尝试全部候选模型）: {last_err}")
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json(content)
    parsed["_llm_model"] = cfg["model"]
    return parsed, data.get("usage", {})


def _get_video_model_config(model_name: str) -> dict:
    """根据模型名返回视频模型配置。"""
    for m in VIDEO_MODELS:
        if m["value"] == model_name:
            return m
    return {}


# 分辨率标签 → 视频尺寸（万相 2.1/2.2/2.5 系列）
_VIDEO_RES_SIZE = {"480P": "832*480", "720P": "1280*720", "1080P": "1920*1080"}
# 固定时长的模型：不受支持的自由时长会强制使用固定值，避免 API 报错
_VIDEO_FIXED_DURATION = {"wan2.2-t2v-plus": 5, "wanx2.1-t2v-turbo": 5}


def _wrap_text(text: str, max_chars: int = 28) -> str:
    """把英文旁白按单词拆成多行，避免字幕溢出画面。"""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _pick_font() -> str:
    """挑选一个可用的系统字体路径供 drawtext 使用（跨平台）。

    Linux 下载体中文字体在前（当前字幕为英文，置前是为中文扩展预留），
    英文字体兜底；全部缺失时返回空串（不阻断，仅无字体渲染）。
    """
    import os as _os
    if sys.platform == "win32":
        candidates = [
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
        ]
    else:
        candidates = [
            # 中文字体在前（当前字幕为英文，置前是为中文扩展预留）
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            # 英文字体兜底
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    for f in candidates:
        if _os.path.exists(f):
            return f.replace("\\", "/")
    return ""


@functools.lru_cache(maxsize=None)
def _ffmpeg_has_drawtext(exe: str) -> bool:
    """探测 ffmpeg 是否编译了 drawtext 滤镜（依赖 libfreetype），结果进程内缓存。"""
    import subprocess as _sp
    try:
        r = _sp.run([exe, "-hide_banner", "-filters"],
                    capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and "drawtext" in r.stdout
    except Exception:
        return False


def _pick_ffmpeg_exe() -> str:
    """优先返回支持 drawtext 的系统 ffmpeg；否则回退 imageio-ffmpeg 自带二进制。"""
    import shutil as _shutil
    sys_ff = _shutil.which("ffmpeg")
    if sys_ff and _ffmpeg_has_drawtext(sys_ff):
        logger.info("ffmpeg 使用系统版本: %s", sys_ff)
        return sys_ff
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info("ffmpeg 回退 imageio-ffmpeg: %s", exe)
        return exe
    except Exception:
        logger.warning("未找到可用 ffmpeg（系统缺失且 imageio-ffmpeg 不可用）")
        return sys_ff or "ffmpeg"


def mux_video_with_audio(video_path: str, audio_bytes: bytes, subtitle_text: str, output_path: str) -> None:
    """用 ffmpeg 把 TTS 旁白合成进无声视频，并烧录英文字幕。

    必须使用完整版 ffmpeg（含 drawtext/libfreetype 与 aac 编码器）。
    ffmpeg 选择策略：优先系统 ffmpeg 且带 drawtext（_pick_ffmpeg_exe），
    imageio-ffmpeg 自带二进制仅作回退。
    """
    import subprocess
    import tempfile
    import os as _os
    import shutil as _shutil

    ffmpeg_exe = _pick_ffmpeg_exe()

    workdir = tempfile.mkdtemp(prefix="toeic_video_")
    audio_name = "audio.mp3"
    sub_name = "sub.txt"

    with open(_os.path.join(workdir, audio_name), "wb") as f:
        f.write(audio_bytes)
    # drawtext textfile 中 % 需转义为 %%，反斜杠需转义
    with open(_os.path.join(workdir, sub_name), "w", encoding="utf-8") as f:
        f.write(_wrap_text(subtitle_text).replace("%", "%%").replace("\\", "\\\\"))

    fontfile = _pick_font()
    drawtext = (
        f"drawtext=textfile='{sub_name}'"
        f":fontcolor=white:fontsize=26:line_spacing=10"
        f":box=1:boxcolor=black@0.55:boxborderw=14"
        f":x=(w-text_w)/2:y=h-th-70:shadowcolor=black@0.5:shadowx=2:shadowy=2"
    )
    if fontfile:
        # 字体路径含盘符冒号，需在 f-string 之外转义为 \: 供 ffmpeg 过滤器解析
        fontfile_escaped = fontfile.replace(":", r"\:")
        drawtext += f":fontfile='{fontfile_escaped}'"

    cmd = [
        ffmpeg_exe, "-y",
        "-i", video_path,
        "-i", audio_name,
        "-filter_complex", f"[0:v]{drawtext}[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir)
    _shutil.rmtree(workdir, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 合成失败: {proc.stderr[-500:]}")


async def call_video_generation(prompt: str, model: str, duration: int = 5) -> bytes:
    """调用百炼文生视频异步接口并轮询，返回视频二进制。"""
    if not VIDEO_API_KEY:
        raise HTTPException(500, "请先设置百炼 API Key（IMAGE_API_KEY / VIDEO_API_KEY）")
    cfg = _get_video_model_config(model)
    resolution = cfg.get("resolution", "480P")
    size = _VIDEO_RES_SIZE.get(resolution, "832*480")
    eff_duration = _VIDEO_FIXED_DURATION.get(model, duration)
    t0 = time.monotonic()
    logger.info("文生视频调用 [视频编译] model=%s resolution=%s duration=%s", model, resolution, eff_duration)

    submit_url = f"{VIDEO_BASE_URL}/services/aigc/video-generation/video-synthesis"
    payload = {
        "model": model,
        "input": {"prompt": prompt},
        "parameters": {"size": size, "duration": eff_duration},
    }
    headers = {
        "Authorization": f"Bearer {VIDEO_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(submit_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    task_id = data.get("output", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"文生视频提交任务失败: {data.get('message', '')}")

    task_url = f"{VIDEO_BASE_URL}/tasks/{task_id}"
    headers_poll = {"Authorization": f"Bearer {VIDEO_API_KEY}"}
    for _ in range(120):  # 视频生成通常 1-5 分钟
        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(task_url, headers=headers_poll)
            resp.raise_for_status()
            tdata = resp.json()
        status = tdata.get("output", {}).get("task_status", "")
        if status == "SUCCEEDED":
            video_url = tdata.get("output", {}).get("video_url")
            if not video_url:
                raise RuntimeError("文生视频任务成功但无 video_url")
            async with httpx.AsyncClient(timeout=120.0) as client:
                vresp = await client.get(video_url)
                vresp.raise_for_status()
            # 视频用量：按本次实际生成秒数记录
            record_model_usage("video", model, resolution, eff_duration)
            logger.info("文生视频成功 [视频编译] model=%s resolution=%s duration=%s 耗时=%.0fs", model, resolution, eff_duration, time.monotonic() - t0)
            return vresp.content
        elif status == "FAILED":
            msg = tdata.get("output", {}).get("message", "未知错误")
            logger.error("文生视频任务失败 [视频编译] model=%s error=%s", model, msg)
            raise RuntimeError(f"文生视频任务失败: {msg}")
    logger.error("文生视频任务超时 [视频编译] model=%s（10分钟未完成）", model)
    raise RuntimeError("文生视频任务超时（10分钟未完成）")


# ========================================================================
# LLM 模型路由（设置页可切换每个调用点使用的模型）
# ========================================================================

def resolve_llm_model(model_value: str) -> dict | None:
    """按模型 value 返回其调用参数 dict（base_url/api_key/model/label/price/note）。"""
    return LLM_MODEL_BY_VALUE.get(model_value)


def get_route_llm(route_key: str) -> dict:
    """返回某 LLM 调用点当前选定的模型配置；未配置或非法时回退到该调用点的默认模型。"""
    from db import get_setting
    value = get_setting(f"llm_route.{route_key}", "") or LLM_ROUTE_DEFAULT.get(route_key, "bailian-qwen3.7-flash")
    cfg = LLM_MODEL_BY_VALUE.get(value) or LLM_MODEL_BY_VALUE.get(LLM_ROUTE_DEFAULT.get(route_key, "bailian-qwen3.7-flash"))
    return cfg or LLM_MODELS[0]


def route_llm_candidates(route_key: str) -> list[dict]:
    """返回某调用点的模型候选链：先当前选定，若与默认不同再附加默认模型作为兜底。
    这样用户把调用点切到其他模型后，一旦其限流或失败，能自动降级到默认模型（百炼 Qwen3.7-Flash），避免直接 500。"""
    primary = get_route_llm(route_key)
    default_val = LLM_ROUTE_DEFAULT.get(route_key, "bailian-qwen3.7-flash")
    if primary["value"] != default_val:
        fallback = LLM_MODEL_BY_VALUE.get(default_val)
        if fallback:
            return [primary, fallback]
    return [primary]


def _estimate_tokens(text: str) -> int:
    """粗略预估文本 token 数：中文约 1.5 token/字，英文约 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = len(text) - cjk
    return max(1, int(cjk * 1.5 + other / 4))


async def _chat_completion(base_url: str, api_key: str, model: str, payload: dict, timeout: float = 120.0, detail: str = "") -> dict:
    """统一的 OpenAI 兼容 chat/completions 调用，返回完整响应 data。
    所有 LLM 调用的日志收口：成功时统一输出 [功能] + 模型 + tokens + 耗时。"""
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={**payload, "model": model},
        )
        resp.raise_for_status()
        data = resp.json()
        # 优先取真实 usage，缺省时按输入文本粗估
        tokens = (data.get("usage", {}) or {}).get("total_tokens") or 0
        if not tokens:
            tokens = _estimate_tokens(" ".join(str(m.get("content", "")) for m in payload.get("messages", [])))
        record_model_usage("llm", model, detail, tokens)
        logger.info("LLM 调用成功 [%s] model=%s tokens=%s 耗时=%.1fs", detail or "未标注功能", model, tokens, time.monotonic() - t0)
        return data


# ========================================================================
# LLM 双模型兜底调用（先试调用点选定的模型，失败降级到该调用点的默认模型）
# ========================================================================

async def _call_llm_with_fallback(
    messages: list[dict],
    route_key: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    response_format: dict | None = None,
    timeout: float = 30.0,
    detail: str = "",
) -> dict | None:
    """调用 LLM，先试该调用点选定的模型，失败时降级到该调用点的默认模型（百炼 Qwen3.7-Flash）。
    返回解析后的 data dict；两者都失败时返回 None。"""
    payload: dict = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    feature = detail or route_key

    # 1) 尝试该调用点选定的模型
    cfg = get_route_llm(route_key)
    if cfg.get("api_key"):
        logger.info("LLM 调用开始 [%s] model=%s(%s) route=%s", feature, cfg["model"], cfg["value"], route_key)
        try:
            return await _chat_completion(cfg["base_url"], cfg["api_key"], cfg["model"], payload, timeout=timeout, detail=detail)
        except Exception as e:
            logger.warning("LLM 调用失败 [%s] model=%s error=%r，准备降级", feature, cfg["model"], e)

    # 2) 降级到该调用点的默认模型（百炼 Qwen3.7-Flash）
    default_value = LLM_ROUTE_DEFAULT.get(route_key)
    default_cfg = LLM_MODEL_BY_VALUE.get(default_value)
    if default_cfg and default_cfg.get("api_key") and default_cfg["value"] != cfg["value"]:
        logger.warning("LLM 降级 [%s] model=%s -> 默认兜底模型 %s", feature, cfg["model"], default_cfg["model"])
        try:
            return await _chat_completion(default_cfg["base_url"], default_cfg["api_key"], default_cfg["model"], payload, timeout=timeout, detail=detail)
        except Exception as e:
            logger.error("LLM 调用失败 [%s] 默认兜底模型 %s 也失败 error=%r", feature, default_cfg["model"], e)

    logger.error("LLM 调用失败 [%s] 全部候选模型均无响应（route=%s）", feature, route_key)
    return None


# ========================================================================
# 百炼 TTS 语音合成
# ========================================================================

# Qwen-Audio-TTS 系列走新版统一 HTTP 语音合成通道（SpeechSynthesizer 端点），
# CosyVoice 系列走 dashscope HttpSpeechSynthesizer 客户端。两者 HTTP REST，规避 WebSocket 网络问题。
QWEN_TTS_HTTP_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
QWEN_TTS_MODELS = {"qwen-audio-3.0-tts-plus", "qwen-audio-3.0-tts-flash"}


async def _call_qwen_audio_http(text: str, voice: str, model: str, speed: float, feature: str) -> bytes:
    """调用百炼非实时 HTTP TTS（SpeechSynthesizer 端点）合成 Qwen-Audio 语音，返回 mp3 二进制。
    成功响应为 JSON 结构，音频通过 output.audio.url 下载。"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            QWEN_TTS_HTTP_ENDPOINT,
            headers={"Authorization": f"Bearer {TTS_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": model,
                "input": {
                    "text": text,
                    "voice": voice,
                    "rate": float(speed),
                    "format": "mp3",
                },
            },
        )
        if resp.status_code != 200:
            try:
                detail = resp.json().get("message") or resp.json().get("error") or resp.text
            except Exception:
                detail = resp.text[:300]
            raise HTTPException(500, f"Qwen-Audio TTS 合成失败 ({model}/{voice}): {detail}")

        try:
            data = resp.json()
            audio_url = data["output"]["audio"]["url"]
        except Exception:
            raise HTTPException(500, f"Qwen-Audio TTS 返回异常: {resp.text[:300]}")
        if not audio_url:
            raise HTTPException(500, "Qwen-Audio TTS 未返回音频地址")

        audio_resp = await client.get(audio_url)
        audio_resp.raise_for_status()
        return audio_resp.content


async def call_tts(text: str, voice=None, speed=1.0, model=None, feature: str = "音频合成"):
    """调用百炼 HTTP TTS API 合成语音，返回 mp3 二进制。feature 用于日志标注调用来源功能。
    按模型分派：qwen-audio 系列走 SpeechSynthesizer HTTP 端点，其余（CosyVoice）走 dashscope
    HttpSpeechSynthesizer 客户端。"""
    if not TTS_API_KEY:
        raise HTTPException(500, "请先设置 TTS_API_KEY 环境变量")

    voice_name = voice or TTS_VOICE
    model_name = model or TTS_MODEL
    logger.info("TTS 合成调用 [%s] model=%s voice=%s 字数=%s", feature, model_name, voice_name, len(text))
    t0 = time.monotonic()

    # Qwen-Audio 系列：走新版统一 HTTP SpeechSynthesizer 端点
    if model_name in QWEN_TTS_MODELS:
        try:
            audio_bytes = await _call_qwen_audio_http(text, voice_name, model_name, speed, feature)
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Qwen-Audio TTS 合成请求失败 [%s] model=%s error=%r", feature, model_name, e)
            raise HTTPException(500, f"Qwen-Audio TTS 合成请求失败 ({model_name}/{voice_name}): {e}")
        record_model_usage("tts", model_name, f"{feature} · 音色 {voice_name}", _estimate_tokens(text))
        logger.info("TTS 合成成功 [%s] model=%s voice=%s 耗时=%.1fs", feature, model_name, voice_name, time.monotonic() - t0)
        return audio_bytes

    # CosyVoice 系列：走 dashscope HttpSpeechSynthesizer 客户端
    dashscope.api_key = TTS_API_KEY
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
        logger.error("TTS 合成请求失败 [%s] model=%s voice=%s error=%r", feature, model_name, voice_name, e)
        raise HTTPException(500, f"TTS 合成请求失败 ({model_name}/{voice_name}): {e}")

    if not result or not result.audio_url:
        msg = (result.message or "返回空结果") if result else "返回空结果"
        raise HTTPException(500, f"TTS 合成失败: {msg}")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            audio_resp = await client.get(result.audio_url)
            audio_resp.raise_for_status()
            # TTS 用量按合成文本预估 token 数
            record_model_usage("tts", model_name, f"{feature} · 音色 {voice_name}", _estimate_tokens(text))
            logger.info("TTS 合成成功 [%s] model=%s voice=%s 耗时=%.1fs", feature, model_name, voice_name, time.monotonic() - t0)
            return audio_resp.content
    except Exception as e:
        logger.error("TTS 音频下载失败 [%s] model=%s error=%r", feature, model_name, e)
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
    """千问图像系列：qwen-image-3.0/2.0-pro、qwen-image、z-image-turbo，同步端点。
    size 与 prompt_extend 从模型配置读取。"""
    cfg = _get_image_model_config(model)
    size = cfg.get("size", "1024*1024")
    prompt_extend = cfg.get("prompt_extend", True)
    url = f"{IMAGE_BASE_URL}/services/aigc/multimodal-generation/generation"
    payload = {
        "model": cfg.get("api_model", model),
        "input": {
            "messages": [{"role": "user", "content": [{"text": prompt}]}]
        },
        "parameters": {"size": size, "n": 1, "prompt_extend": prompt_extend},
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
    """万相文生图：wan2.7/2.6-image、wan2.2-t2i 等，异步轮询端点。size 从模型配置读取。"""
    cfg = _get_image_model_config(model)
    size = cfg.get("size", "1280*720")
    submit_url = f"{IMAGE_BASE_URL}/services/aigc/text2image/image-synthesis"
    payload = {
        "model": cfg.get("api_model", model),
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


async def _generate_image_openai_compat(prompt: str, model: str, api_key: str, base_url: str, size: str = "1024x1024") -> bytes:
    """OpenAI 兼容协议文生图（如 TokenRhythm），POST {base_url}/images/generations。
    优先用 b64_json 直接返回图片数据（避免二次下载 OSS 签名 URL 的间歇性 403）；
    若平台仅返回 url，则带浏览器 UA 重试下载。
    对 502/503/504 及超时自动重试 3 次（退避），缓解平台抖动。"""
    url = f"{base_url}/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "b64_json",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 临时性服务端错误：502/503/504，及网络超时 —— 自动重试
    RETRYABLE_STATUS = {502, 503, 504}
    last_err = None
    data = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in RETRYABLE_STATUS:
                last_err = RuntimeError(f"平台临时不可用 HTTP {resp.status_code}")
                await asyncio.sleep(2.0 * (attempt + 1))  # 2s, 4s, 6s 退避
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = e
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
    if data is None:
        raise RuntimeError(f"文生图请求失败（重试 3 次）: {last_err}")

    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"文生图返回无 data: {data}")
    item = items[0]

    # 1) 优先 b64_json（无需二次下载，最可靠）
    b64 = item.get("b64_json")
    if b64:
        return base64.b64decode(b64)

    # 2) 退回到 url 下载（带浏览器 UA，重试 3 次以规避 OSS 签名 URL 间歇 403）
    image_url = item.get("url")
    if not image_url:
        raise RuntimeError("文生图返回无 url/b64_json")
    dl_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
    last_err = None
    for attempt in range(3):
        try:
            await asyncio.sleep(1.0 * attempt)  # 首次立即，后续指数退避
            async with httpx.AsyncClient(timeout=60.0) as client:
                img_resp = await client.get(image_url, headers=dl_headers)
                img_resp.raise_for_status()
                return img_resp.content
        except Exception as e:
            last_err = e
    raise RuntimeError(f"图片下载失败（重试 3 次）: {last_err}")


async def call_image_generation(prompt: str, model: str = None, feature: str = "") -> bytes:
    """文生图统一入口，按模型分派到同步、异步或 OpenAI 兼容端点。返回图片二进制。
    feature: 调用来源的编译功能名（如「批量编译/单点深耕/场景编译」），
             用于用量明细的「说明」字段，缺省为空（前端兜底展示「文生图」）。"""
    model_name = model or IMAGE_MODEL
    cfg = _get_image_model_config(model_name)
    endpoint = cfg.get("endpoint", "t2i")
    provider = cfg.get("provider", "dashscope")
    api_model = cfg.get("api_model", model_name)
    t0 = time.monotonic()
    logger.info("文生图调用 model=%s(value=%s) provider=%s endpoint=%s feature=%s", api_model, model_name, provider, endpoint, feature or "—")
    try:
        if endpoint == "openai":
            # OpenAI 兼容协议（TokenRhythm 免费调用 qwen-image-2.0 / wan2.7-image）
            if provider == "tokenrhythm":
                if not TOKENRHYTHM_API_KEY:
                    logger.error("TokenRhythm 免费文生图缺少 TOKENRHYTHM_API_KEY（model=%s）", api_model)
                    raise HTTPException(500, "请先设置 TOKENRHYTHM_API_KEY 环境变量")
                api_key, base_url = TOKENRHYTHM_API_KEY, TOKENRHYTHM_BASE_URL
            else:
                if not IMAGE_API_KEY:
                    raise HTTPException(500, "请先设置 IMAGE_API_KEY 或 TTS_API_KEY 环境变量")
                api_key, base_url = IMAGE_API_KEY, IMAGE_BASE_URL
            data = await _generate_image_openai_compat(
                prompt, api_model, api_key, base_url, cfg.get("size", "1024x1024")
            )
        elif not IMAGE_API_KEY:
            logger.error("文生图缺少 IMAGE_API_KEY/TTS_API_KEY（model=%s）", model_name)
            raise HTTPException(500, "请先设置 IMAGE_API_KEY 或 TTS_API_KEY 环境变量")
        elif endpoint == "multimodal":
            data = await _generate_image_qwen_multimodal(prompt, model_name)
        else:
            data = await _generate_image_wan_t2i(prompt, model_name)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("文生图调用失败 model=%s(value=%s) endpoint=%s provider=%s error=%r", api_model, model_name, endpoint, provider, e)
        raise HTTPException(500, f"文生图失败 ({model_name}): {e}")
    # 文生图用量：1 次调用 = 生成 1 张图；detail 标注来自哪个编译功能（与 LLM/TTS 一致）
    record_model_usage("image", model_name, feature or "", 1)
    logger.info("文生图成功 model=%s(value=%s) feature=%s 耗时=%.1fs", api_model, model_name, feature or "—", time.monotonic() - t0)
    return data


_STYLE_IMAGE_PROMPT_PREFIX = {
    "absurd": "Surreal comic, absurd juxtaposition, bold flat colors, weird objects",
    "conflict": "Comic strip, exaggerated facial expressions, two-character interaction, office satire",
    "scene": "Coherent cartoon, consistent characters, clean flat colors, corporate/office setting",
}


async def generate_panel_image(prompt: str, model: str, gen_id: str, scene_index: int, style: str = "", art_style: str = "", feature: str = "") -> dict:
    """为单个画面生成图片，失败时降级（不阻塞整体）。
    style: ''/'legacy' 保留旧版微电影风格；'absurd'/'conflict' 强化对应漫画风格，避免被电影质感污染。
    art_style: 'auto' 时跳过固定风格前缀，完全由 LLM 在 image_prompt 中自主选择画风。
    feature: 编译功能名（如「批量编译/场景编译」），用于用量明细的「说明」字段。"""
    if art_style == "auto":
        full_prompt = f"{prompt}, 16:9"
    else:
        prefix = _STYLE_IMAGE_PROMPT_PREFIX.get(style)
        if prefix:
            full_prompt = f"{prefix}, {prompt}, 16:9"
        else:
            full_prompt = f"cinematic storyboard, film grain, dramatic lighting, {prompt}, 16:9"
    try:
        img_bytes = await call_image_generation(full_prompt, model, feature=feature)
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
    """调用 LLM（熟词僻意路由，可切换模型）批量判断单词是否为托业高频熟词僻意，返回结构化词条。
    选定模型调用失败（如限流）时自动降级到默认主模型。"""
    if not words:
        return {"results": []}

    user_prompt = _build_polysemy_detect_prompt(words)
    payload = {
        "messages": [
            {"role": "system", "content": POLYSEMY_DETECT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    last_err = None
    for cfg in route_llm_candidates("polysemy"):
        if not cfg.get("api_key"):
            continue
        logger.info("LLM 熟词僻意检测调用 model=%s(%s) words=%s", cfg["model"], cfg["value"], len(words))
        try:
            data = await _chat_completion(cfg["base_url"], cfg["api_key"], cfg["model"], payload, timeout=120.0, detail="熟词僻意检测")
            break
        except (httpx.HTTPError, KeyError) as e:
            logger.warning("LLM 熟词僻意检测调用失败 model=%s error=%r，尝试下一候选模型", cfg["model"], e)
            last_err = e
            continue
    else:
        if last_err is None:
            logger.error("LLM 熟词僻意检测失败：未找到已配置 API Key 的可用模型")
            raise HTTPException(500, "未找到已配置 API Key 的可用模型，请检查环境变量或更换模型")
        logger.error("LLM 熟词僻意检测失败（已尝试全部候选模型）: %r", last_err)
        raise HTTPException(500, f"LLM 熟词僻意检测失败（已尝试全部候选模型）: {last_err}")

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
# 构词拆解：批量判定可拆词（扫描）
# ========================================================================

MORPHEME_SYSTEM = """You are an English morphology analyzer for TOEIC vocabulary. Given a list of English words, determine whether each can be clearly decomposed into morphemes (prefix + root + suffix) with high confidence.

Rules:
- Only mark is_decomposable=true when the decomposition is CERTAIN and the word is clearly built from identifiable morphemes (e.g. brokerage = broker + -age, management = manage + -ment, reconsider = re- + consider).
- Do NOT force-decompose words whose etymology is opaque (e.g. business, office, important) — mark is_decomposable=false.
- For decomposable words, fill ALL fields:
  * stem: the base word (e.g. broker)
  * stem_zh: Chinese meaning of the stem (e.g. 经纪人)
  * affixes: list of {affix, type, meaning}. type is one of prefix|root|suffix; meaning is the Chinese meaning of that affix.
  * structure_code: readable formula, e.g. "broker + -age"
  * root: the tree-building morpheme. Prefer the affix axis (prefix/suffix) when present, e.g. "-age".
  * root_zh: Chinese meaning of root.
  * root_type: "prefix" | "root" | "suffix".
  * word_family: 2-6 related words sharing the same root/morpheme.
- For non-decomposable words, return ONLY word + is_decomposable=false, nothing else.

Return a single JSON object matching the schema provided."""


def _build_morpheme_detect_prompt(words: list[str]) -> str:
    numbered = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    return f"""Analyze each of the following {len(words)} English words.

WORDS:
{numbered}

For each word return:
- is_decomposable: true only if the word is clearly built from identifiable morphemes.
- If decomposable: stem, stem_zh, affixes[{{affix,type,meaning}}], structure_code, root, root_zh, root_type, word_family.
- If not decomposable: only word + is_decomposable=false.

Return a single JSON object matching the schema provided."""


async def call_morpheme_detect(words: list[str]):
    """调用 LLM（构词拆解路由，可切换模型）批量判定单词是否可拆并给出结构。
    选定模型调用失败（如限流）时自动降级到默认主模型。"""
    if not words:
        return {"results": []}

    user_prompt = _build_morpheme_detect_prompt(words)
    payload = {
        "messages": [
            {"role": "system", "content": MORPHEME_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    last_err = None
    for cfg in route_llm_candidates("morpheme"):
        if not cfg.get("api_key"):
            continue
        logger.info("LLM 构词拆解调用 model=%s(%s) words=%s", cfg["model"], cfg["value"], len(words))
        try:
            data = await _chat_completion(cfg["base_url"], cfg["api_key"], cfg["model"], payload, timeout=120.0, detail="构词拆解判定")
            break
        except (httpx.HTTPError, KeyError) as e:
            logger.warning("LLM 构词拆解调用失败 model=%s error=%r，尝试下一候选模型", cfg["model"], e)
            last_err = e
            continue
    else:
        if last_err is None:
            logger.error("LLM 构词拆解失败：未找到已配置 API Key 的可用模型")
            raise HTTPException(500, "未找到已配置 API Key 的可用模型，请检查环境变量或更换模型")
        logger.error("LLM 构词拆解失败（已尝试全部候选模型）: %r", last_err)
        raise HTTPException(500, f"LLM 构词拆解失败（已尝试全部候选模型）: {last_err}")

    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(500, "LLM 返回格式非 JSON，无法解析构词拆解结果")

    # 兼容 LLM 的多种返回形态：顶层数组 / {"results": [...]} / {"words": [...]}
    if isinstance(parsed, list):
        results = parsed
    else:
        results = parsed.get("results") or parsed.get("words") or []
    cleaned = []
    seen = set()
    for r in results:
        w = str(r.get("word", "")).strip().lower()
        if not w or w in seen:
            continue
        seen.add(w)
        if r.get("is_decomposable") is True:
            affixes = []
            for a in (r.get("affixes") or []):
                if isinstance(a, dict) and str(a.get("affix", "")).strip():
                    affixes.append({
                        "affix": str(a.get("affix", ""))[:40],
                        "type": str(a.get("type", ""))[:16],
                        "meaning": str(a.get("meaning", ""))[:80],
                    })
            family = [str(x).strip().lower() for x in (r.get("word_family") or []) if isinstance(x, str) and x.strip()][:8]
            cleaned.append({
                "word": w,
                "is_decomposable": True,
                "stem": str(r.get("stem", ""))[:60],
                "stem_zh": str(r.get("stem_zh", ""))[:80],
                "affixes": affixes,
                "structure_code": str(r.get("structure_code", ""))[:120],
                "root": str(r.get("root", ""))[:40],
                "root_zh": str(r.get("root_zh", ""))[:80],
                "root_type": str(r.get("root_type", ""))[:16],
                "word_family": family,
            })
        else:
            cleaned.append({"word": w, "is_decomposable": False})
    return {"results": cleaned, "usage": data.get("usage", {}), "model": str(data.get("model", ""))}


# ========================================================================
# 构词拆解：词根树推荐同构词（懒填充 / 添加成员，P2 专用）
# ========================================================================

MORPHEME_SEED_SYSTEM = """You are a TOEIC vocabulary expert. Given a morpheme/root (e.g. "-age") and a list of words already associated with it, recommend additional TOEIC core words built with the SAME morpheme.

Rules:
- Return EXACTLY 3 words. If you cannot honestly find 3, return 0-2 and explain why in "reason". Never fabricate or pad.
- Every word must be TOEIC core vocabulary and must contain the morpheme with the same construction (e.g. for "-age": postage, storage, coverage).
- Order by exam frequency: the most important words first.
- For each word output word + meaning_zh + frequency_level (★ to ★★★★★).
- Do NOT recommend any word that already appears in the given existing list.

Output ONLY a JSON object: {"recommended": [{"word","meaning_zh","frequency_level"}], "reason": ""}"""


def _build_morpheme_seed_prompt(root: str, root_zh: str, root_type: str, existing: list[str]) -> str:
    existing_txt = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(existing)) if existing else "  （无）"
    return f"""MORPHEME: {root}
MEANING: {root_zh}
TYPE: {root_type}

WORDS ALREADY ASSOCIATED (do NOT recommend these):
{existing_txt}

Recommend exactly 3 NEW TOEIC core words containing this morpheme, sorted by exam frequency (most important first).
Return a JSON object matching the schema provided."""


async def call_morpheme_seed(root: str, root_zh: str, root_type: str, existing: list[str]):
    """调用 LLM（构词拆解·词根推荐路由）为该词根生成 3 个 P2 推荐词。
    失败时返回 None（调用方给出明确提示，不静默）。"""
    user_prompt = _build_morpheme_seed_prompt(root, root_zh, root_type, existing)
    messages = [
        {"role": "system", "content": MORPHEME_SEED_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    data = await _call_llm_with_fallback(
        messages=messages,
        route_key="morpheme_seed",
        temperature=0.3,
        max_tokens=2048,
        response_format={"type": "json_object"},
        timeout=60.0,
        detail="构词拆解·词根推荐",
    )
    if data is None:
        return None

    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None

    # 兼容 LLM 的多种返回形态：顶层数组 / {"recommended": [...]} / {"words": [...]}
    if isinstance(parsed, list):
        rec_list = parsed
    else:
        rec_list = parsed.get("recommended") or parsed.get("words") or []
    recommended = []
    seen = set()
    for r in rec_list:
        if not isinstance(r, dict):
            continue
        w = str(r.get("word", "")).strip().lower()
        if not w or w in seen or w in {x.lower() for x in existing}:
            continue
        seen.add(w)
        recommended.append({
            "word": w,
            "meaning_zh": str(r.get("meaning_zh", ""))[:200],
            "frequency_level": str(r.get("frequency_level", ""))[:16],
        })
    reason = "" if isinstance(parsed, list) else str(parsed.get("reason", ""))[:300]
    return {"recommended": recommended, "reason": reason}


# ========================================================================
# 单词词性/释义自动补充
# ========================================================================

WORD_ENRICH_SYSTEM = """You are an English vocabulary assistant for TOEIC learners. Given a list of English words, return the part of speech, a comprehensive Chinese meaning and the TOEIC exam frequency for each word.

Rules:
- Part of speech (pos): use short labels like v., n., adj., adv., prep., conj., pron., etc. If a word has multiple common POS, list the most important ones separated by "/" (e.g. "v./n.").
- Chinese meaning (meaning_zh): provide a comprehensive yet concise Chinese definition. Include the most common meanings used in business/workplace contexts. Keep it under 80 characters.
- frequency_level: estimate the word's importance in the TOEIC exam as a star rating from ★ to ★★★★★ (5 stars = extremely frequent/important, 1 star = rare). Use the ★ character repeated 1-5 times.
- For words that are already in the input (e.g. inflected forms), return the base form's info.

Output ONLY a valid JSON object. No markdown, no extra text.

JSON STRUCTURE:
{
  "results": [
    {
      "word": "accommodate",
      "pos": "v.",
      "meaning_zh": "容纳；为…提供住宿；适应，顺应",
      "frequency_level": "★★★★★"
    },
    {
      "word": "negotiate",
      "pos": "v.",
      "meaning_zh": "谈判，协商；商议（条件）；顺利通过",
      "frequency_level": "★★★★☆"
    }
  ]
}"""


def _build_enrich_prompt(words: list[str]) -> str:
    numbered = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
    return f"""Please provide the part of speech, Chinese meaning and TOEIC exam frequency for each of the following {len(words)} English words.

WORDS:
{numbered}

For each word, return:
- pos: part of speech label (e.g. v., n., adj., adv., or combined like "v./n.")
- meaning_zh: comprehensive Chinese definition (max 80 characters)
- frequency_level: star rating ★ to ★★★★★ indicating TOEIC exam importance

Return a single JSON object matching the schema provided."""


async def call_word_enrichment(words: list[str]) -> dict:
    """调用 LLM 批量补充单词的词性、中文释义和频率（走该调用点模型，失败降级默认模型）。"""
    if not words:
        return {"results": []}
    if not get_route_llm("enrich").get("api_key"):
        return {"results": [], "skipped": True, "reason": "no_api_key"}

    user_prompt = _build_enrich_prompt(words)
    messages = [
        {"role": "system", "content": WORD_ENRICH_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    data = await _call_llm_with_fallback(
        messages=messages,
        route_key="enrich",
        temperature=0.2,
        max_tokens=2048,
        response_format={"type": "json_object"},
        timeout=30.0,
        detail="单词补充",
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
            "frequency_level": str(r.get("frequency_level", ""))[:16],
        })
    return {"results": cleaned, "skipped": False}


# ========================================================================
# 文章提词（内容导入生岛：粘贴文章 → 提取值得学习的单词）
# ========================================================================

WORD_EXTRACT_SYSTEM = """You are a vocabulary curator for an English-learning app. Given an English article (tech blog / README / changelog / docs / news / any prose), extract the words MOST WORTH LEARNING for a Chinese learner.

SELECTION CRITERIA (strict):
1. Exclude: CET-4 level common words, function words, proper nouns, brand/product names, code identifiers (variable/function names), numbers.
2. Include: CET-6 / TOEFL / IELTS / GRE level words, technical jargon with general value (e.g. deprecated, idempotent, latency, throughput, bottleneck), precise verbs/adjectives used well in context, and words whose meaning a learner likely cannot guess.
3. Prefer words that actually APPEAR in the article; each word must come with its original sentence from the article as context.
4. Rank by learning value; return at most the requested count.

OUTPUT ONLY a valid JSON object, no markdown:
{
  "results": [
    {
      "word": "deprecated",
      "pos": "adj.",
      "meaning_zh": "已弃用的；不赞成的",
      "context_en": "The original sentence from the article containing the word.",
      "context_zh": "该句中文翻译"
    }
  ]
}"""


async def call_word_extraction(text: str, max_words: int = 12) -> dict:
    """从粘贴的文章中提取值得学习的单词（走 extract 调用点，失败降级默认模型）。
    返回 {results: [{word, pos, meaning_zh, context_en, context_zh}], skipped, reason}。"""
    text = (text or "").strip()
    if not text:
        return {"results": [], "skipped": True, "reason": "empty_text"}
    # 截断超长文章：保留开头 12000 字符足以覆盖提取需求，也控制 token 成本
    if len(text) > 12000:
        text = text[:12000]
    if not get_route_llm("extract").get("api_key"):
        return {"results": [], "skipped": True, "reason": "no_api_key"}

    user_prompt = f"""ARTICLE:
{text}

MAX WORDS: {max_words}

Extract up to {max_words} words most worth learning, with each word's original article sentence as context. Output ONLY the JSON object."""

    messages = [
        {"role": "system", "content": WORD_EXTRACT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    data = await _call_llm_with_fallback(
        messages=messages,
        route_key="extract",
        temperature=0.3,
        max_tokens=4096,
        response_format={"type": "json_object"},
        timeout=60.0,
        detail="文章提词",
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

    cleaned, seen = [], set()
    for r in parsed.get("results", []):
        w = str(r.get("word", "")).strip().lower()
        # 提词场景只收纯英文单词（允许连字符/撇号），短语/搭配剔除
        if not w or not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", w) or w in seen:
            continue
        seen.add(w)
        cleaned.append({
            "word": w,
            "pos": str(r.get("pos", ""))[:20],
            "meaning_zh": str(r.get("meaning_zh", ""))[:200],
            "context_en": str(r.get("context_en", ""))[:400],
            "context_zh": str(r.get("context_zh", ""))[:200],
        })
    return {"results": cleaned, "skipped": False}


# ========================================================================
# 单词音标（LLM 补全）
# ========================================================================

PHONETIC_SYSTEM = """You are an English pronunciation assistant. Given an English word, return its standard American English IPA phonetic transcription.
Rules:
- Use standard IPA notation between slashes, e.g. /ˈsuːpərvaɪz/.
- Use the main/primary pronunciation for the most common sense of the word.
- Handle inflected forms (e.g. supervise → its base pronunciation is still /ˈsuːpərvaɪz/).
Output ONLY a valid JSON object, no markdown, no extra text.

JSON STRUCTURE:
{"word": "supervise", "phonetic": "/ˈsuːpərvaɪz/"}"""


async def call_word_phonetic(word: str) -> str:
    """调用 LLM 补全单个单词音标（走该调用点模型，失败降级默认模型）。成功返回 IPA 字符串，失败返回空串。"""
    word = str(word or "").strip().lower()
    if not word:
        return ""
    if not get_route_llm("enrich").get("api_key"):
        return ""
    messages = [
        {"role": "system", "content": PHONETIC_SYSTEM},
        {"role": "user", "content": f"WORD: {word}"},
    ]
    data = await _call_llm_with_fallback(
        messages=messages,
        route_key="enrich",
        temperature=0.0,
        max_tokens=64,
        response_format={"type": "json_object"},
        timeout=20.0,
        detail="单词音标",
    )
    if data is None:
        return ""
    content = (data["choices"][0]["message"]["content"] or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        parsed = json.loads(content)
        ph = str(parsed.get("phonetic", "")).strip()
        return ph
    except (json.JSONDecodeError, AttributeError, ValueError):
        return ""


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
    if not get_route_llm("scene_detect").get("api_key"):
        raise HTTPException(500, "未配置 LLM API Key（默认使用百炼 Qwen3.7-Flash，请配置 IMAGE_API_KEY / TTS_API_KEY）")

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
        route_key="scene_detect",
        temperature=0.2,
        max_tokens=8192,
        response_format={"type": "json_object"},
        timeout=90.0,
        detail="场景检测",
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


# ========================================================================
# 场景聚汇：场景词伙搭配生成
# ========================================================================

SCENE_COLLOCATIONS_SYSTEM_PROMPT = """You are a TOEIC collocation expert. Generate typical high-frequency business collocations for the words in a scene.

OUTPUT JSON ONLY:
{
  "collocations": [
    {
      "phrase": "submit a resume",
      "zh": "提交简历",
      "words": ["submit", "resume"],
      "example_en": "Please submit a resume before Friday.",
      "example_zh": "请在周五前提交简历。"
    }
  ]
}

RULES:
1. Generate 2-5 collocations. Each phrase must be 2-4 words and contain at least one word from the scene word list.
2. Collocations should be authentic TOEIC business chunks (verb+noun, noun+noun, adj+noun, etc.).
3. zh: concise Chinese meaning of the phrase.
4. words: the scene words used in this collocation.
5. example_en/example_zh: one short example sentence using the collocation.
6. Output only the JSON object."""


def _build_scene_collocations_user_prompt(words: list[str], scene_name: str, scene_name_zh: str) -> str:
    """构造场景词伙生成的用户 prompt。"""
    lines = [f"Scene: {scene_name} ({scene_name_zh})", "Words in this scene:", "  " + ", ".join(words)]
    return "\n".join(lines)


async def call_deepseek_scene_collocations(words: list[str], scene_name: str, scene_name_zh: str = "") -> list[dict]:
    """为场景内单词生成 2-5 条典型 TOEIC 词伙搭配。任何失败都返回 []，不抛异常。"""
    if not words or len(words) < 2:
        return []
    if not get_route_llm("scene_collocations").get("api_key"):
        return []
    messages = [
        {"role": "system", "content": SCENE_COLLOCATIONS_SYSTEM_PROMPT},
        {"role": "user", "content": _build_scene_collocations_user_prompt(words, scene_name, scene_name_zh)},
    ]
    try:
        data = await _call_llm_with_fallback(
            messages=messages,
            route_key="scene_collocations",
            temperature=0.3,
            max_tokens=2048,
            response_format={"type": "json_object"},
            timeout=60.0,
            detail="场景词伙",
        )
        if data is None:
            return []
        content = data["choices"][0]["message"]["content"]
        if not content or not content.strip():
            return []
        parsed = _extract_json(content)
        out = []
        seen = set()
        for c in parsed.get("collocations", [])[:8]:
            phrase = str(c.get("phrase", "")).strip()
            if not phrase or phrase.lower() in seen:
                continue
            seen.add(phrase.lower())
            out.append({
                "phrase_en": phrase,
                "phrase_zh": str(c.get("zh", "")).strip(),
                "words": [str(w).strip().lower() for w in c.get("words", []) if str(w).strip()][:10],
                "example_en": str(c.get("example_en", "")).strip(),
                "example_zh": str(c.get("example_zh", "")).strip(),
            })
        return out
    except Exception:
        return []
