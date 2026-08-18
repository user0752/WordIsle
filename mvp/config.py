"""
TOEIC MVP 配置
==============
所有环境变量、路径、模型常量统一在此管理。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ========================================================================
# 路径
# ========================================================================

BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = BASE_DIR / "data"
DB_PATH    = DATA_DIR / "words.db"
AUDIOS_DIR = DATA_DIR / "audios"
IMAGES_DIR   = DATA_DIR / "images"
VIDEOS_DIR   = DATA_DIR / "videos"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR   = BASE_DIR / "static"
LOG_DIR      = BASE_DIR / "logs"

# ========================================================================
# DeepSeek
# ========================================================================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE    = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL   = os.getenv("DEEPSEEK_MODEL_NAME", "deepseek-chat")

# ========================================================================
# TTS（阿里云百炼语音合成）
# ========================================================================

TTS_API_KEY  = os.getenv("TTS_API_KEY", "")
TTS_VOICE    = os.getenv("TTS_VOICE", "loongandy_v3")
TTS_MODEL    = os.getenv("TTS_MODEL", "cosyvoice-v3-flash")

# 模型 → 默认推荐音色。模型与音色必须同系列，避免不兼容（Qwen 系列用 loongmary/loongeva/loongjohn，
# CosyVoice 系列用 loongandy/loongbeth/loongemily/loongeric）。切换模型时音色应随之切换。
TTS_DEFAULT_VOICE = {
    "qwen-audio-3.0-tts-plus": "loongmary",
    "cosyvoice-v3-plus":       "loongandy_v3",
    "cosyvoice-v3-flash":      "loongandy_v3",
}


def default_tts_voice(model: str) -> str:
    """返回某 TTS 模型的默认音色；未知名则回退到全局默认音色。"""
    return TTS_DEFAULT_VOICE.get(model, TTS_VOICE)

# ========================================================================
# 文生图（阿里云百炼，复用 TTS_API_KEY）
# ========================================================================

IMAGE_API_KEY  = os.getenv("IMAGE_API_KEY", TTS_API_KEY)
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")
IMAGE_MODEL    = os.getenv("IMAGE_MODEL", "wan2.7-image")

# 文生图模型多档阶梯（用户可在前端自由选择，价格依据《阿里云百炼图像生成价格文档》）
# 各档 endpoint：
#   multimodal -> 千问图像系列（同步 generation 端点）
#   t2i        -> 万相文生图（异步轮询 image-synthesis 端点）
#   openai     -> TokenRhythm 平台（OpenAI 兼容协议）
IMAGE_MODELS = [
    # ============ 旗舰档（复杂版面/文字渲染） ============
    {
        "value": "qwen-image-3.0-pro",
        "label": "旗舰 · Qwen-Image 3.0 Pro (文本渲染最强)",
        "tier": "旗舰",
        "price": "0.25 元/张 (1k)",
        "note": "千问3.0旗舰版，agent prompt智能改写，擅长中英文文本渲染；复杂版面首选；10张免费额度",
        "features": "agent prompt智能改写；中英文本渲染最强；复杂版面/小字/多语言字体；旗舰画质",
        "scenarios": "画面需出现准确中英文文字、复杂密集版面时首选",
        "endpoint": "multimodal",
        "size": "1024*1024",
        "prompt_extend": True,
    },
    {
        "value": "qwen-image-2.0-pro-2026-06-22",
        "label": "旗舰 · Qwen-Image 2.0 Pro 0622 (限时免费100张)",
        "tier": "旗舰",
        "price": "0.50 元/张",
        "note": "千问2.0 Pro 2026-06-22 版本，能力同 qwen-image-2.0-pro；限时免费 100 张",
        "features": "千问2.0 Pro能力均衡；文本渲染与复杂版面表现好；限时免费100张",
        "scenarios": "想用Pro画质又省额度时首选；常规高质量记忆插图",
        "endpoint": "multimodal",
        "size": "1024*1024",
        "prompt_extend": True,
    },
    # ============ 高清档（通用高质量） ============
    {
        "value": "qwen-image-3.0",
        "label": "高清 · Qwen-Image 3.0 (均衡)",
        "tier": "高清",
        "price": "0.18 元/张 (2k)",
        "note": "千问3.0标准版，速度快于Pro；文本渲染与复杂版面同样出色；10张免费额度",
        "features": "千问3.0标准；速度快于Pro；文本渲染与复杂版面同样出色；10张免费",
        "scenarios": "通用高质量需求，兼顾速度与画质的默认选择",
        "endpoint": "multimodal",
        "size": "1944*1032",
        "prompt_extend": True,
    },
    # ============ 性价比档（快速低成本） ============
    {
        "value": "z-image-turbo",
        "label": "性价比 · Z-Image Turbo (最快·低价)",
        "tier": "性价比",
        "price": "0.10 元/张 (关改写)",
        "note": "快速低成本，速度比wan2.7快10倍；写实人像和产品照片；仅文生图不支持编辑",
        "features": "速度比wan2.7快约10倍；价格约1/5；写实人像/产品照片表现出色；仅文生图不支持编辑",
        "scenarios": "追求最快与最低成本，或要写实人像、产品照片时",
        "endpoint": "multimodal",
        "size": "1024*1024",
        "prompt_extend": False,
    },
    # ============ 万相·连环画档（角色一致性，适合多画面） ============
    {
        "value": "wan2.7-image",
        "label": "万相 · Wan 2.7 Image (连环画首选)",
        "tier": "万相",
        "price": "0.20 元/张",
        "note": "角色一致性多图生成，连环画人物统一；2K分辨率",
        "features": "角色一致性多图生成；连环画人物统一；2K分辨率",
        "scenarios": "多画面连环画、需要同一角色贯穿全程时首选",
        "endpoint": "multimodal",
        "size": "1280*720",
    },
    # ============ 免费档（TokenRhythm 平台免费调用） ============
    {
        "value": "qwen-image-2.0",
        "api_model": "qwen-image-2.0",
        "label": "免费 · Qwen-Image 2.0 (TokenRhythm·免费)",
        "tier": "免费",
        "price": "免费",
        "note": "TokenRhythm 平台免费调用（OpenAI 兼容协议）；千问图像2.0，文生图最高 2048x2048",
        "features": "TokenRhythm免费；千问图像2.0；文生图最高2048x2048",
        "scenarios": "免费场景、常规记忆插图，成本敏感时首选",
        "endpoint": "openai",
        "provider": "tokenrhythm",
        "size": "1024x1024",
    },
    {
        "value": "wan2.7-image-free",
        "api_model": "wan2.7-image",
        "label": "免费 · Wan 2.7 Image (TokenRhythm·免费)",
        "tier": "免费",
        "price": "免费",
        "note": "TokenRhythm 平台免费调用（OpenAI 兼容协议）；万相2.7，角色一致，适合连环画",
        "features": "TokenRhythm免费；万相2.7；角色一致适合连环画",
        "scenarios": "免费多画面连环画、角色一致性需求",
        "endpoint": "openai",
        "provider": "tokenrhythm",
        "size": "1024x1024",
    },
]

# ========================================================================
# 文生视频模型（视频编译）
# 计费：输出按成功生成的视频秒数计费；失败不收费也不消耗免费额度。
# free=True 表示当前账号有免费额度（quota 为剩余秒数，expiry 为到期日），使用/测试会消耗额度。
# ========================================================================
VIDEO_MODELS = [
    # ============ 付费模型（无免费额度） ============
    {
        "value": "wan2.2-t2v-plus",
        "label": "低成本 · Wan 2.2 T2V Plus (480P)",
        "tier": "付费",
        "price": "0.14 元/秒",
        "resolution": "480P",
        "free": False,
        "note": "万相2.2文生视频Plus，全表最便宜；480P画质较低但适合低成本量产",
    },
    {
        "value": "wanx2.1-t2v-turbo",
        "label": "性价比 · Wanx2.1 T2V Turbo (720P)",
        "tier": "付费",
        "price": "0.24 元/秒",
        "resolution": "720P",
        "free": False,
        "note": "万相2.1文生视频Turbo，速度快、指令遵循强；720P画质与成本均衡",
    },
    # ============ 有免费额度模型（使用/测试会消耗额度） ============
    {
        "value": "wan2.7-t2v-2026-06-12",
        "label": "免费 · Wan 2.7 T2V (文生视频)",
        "tier": "有额度",
        "price": "0.60 元/秒",
        "resolution": "720P",
        "free": True,
        "quota": 50,
        "quota_total": 50,
        "expiry": "2026/09/30",
        "note": "万相2.7文生视频，直接文本出视频；免费额度50秒，使用/测试消耗",
    },
    {
        "value": "wan3.0-video",
        "label": "免费 · Wan 3.0 Video (邀测)",
        "tier": "有额度",
        "price": "0.60 元/秒",
        "resolution": "720P",
        "free": True,
        "quota": 30,
        "quota_total": 30,
        "expiry": "2026/11/05",
        "note": "万相3.0视频生成（邀测中）；免费额度30秒（输入+输出合计），使用/测试消耗",
    },
    {
        "value": "happyhorse-1.1-t2v",
        "label": "免费 · HappyHorse 1.1 T2V (文生视频)",
        "tier": "有额度",
        "price": "0.45 元/秒",
        "resolution": "480P",
        "free": True,
        "quota": 10,
        "quota_total": 10,
        "expiry": "2026/09/21",
        "note": "快乐小马文生视频；免费额度10秒，使用/测试消耗",
    },
    {
        "value": "happyhorse-1.1-i2v",
        "label": "免费 · HappyHorse 1.1 I2V (图生·首帧)",
        "tier": "有额度",
        "price": "0.45 元/秒",
        "resolution": "480P",
        "free": True,
        "quota": 10,
        "quota_total": 10,
        "expiry": "2026/09/21",
        "note": "快乐小马图生视频（基于首帧），需先提供首帧图；免费额度10秒，使用/测试消耗",
    },
    {
        "value": "happyhorse-1.1-r2v",
        "label": "免费 · HappyHorse 1.1 R2V (参考生视频)",
        "tier": "有额度",
        "price": "0.45 元/秒",
        "resolution": "480P",
        "free": True,
        "quota": 10,
        "quota_total": 10,
        "expiry": "2026/09/21",
        "note": "快乐小马参考生视频（需参考视频）；免费额度10秒，使用/测试消耗",
    },
    {
        "value": "wan2.7-r2v-2026-06-12",
        "label": "免费 · Wan 2.7 R2V (参考生视频)",
        "tier": "有额度",
        "price": "0.60 元/秒",
        "resolution": "720P",
        "free": True,
        "quota": 50,
        "quota_total": 50,
        "expiry": "2026/09/30",
        "note": "万相2.7参考生视频（需参考视频）；免费额度50秒，使用/测试消耗",
    },
]

# 文生视频（阿里云百炼，复用百炼 API Key）
VIDEO_API_KEY  = os.getenv("VIDEO_API_KEY", IMAGE_API_KEY)
VIDEO_BASE_URL = os.getenv("VIDEO_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")

# ========================================================================
# TokenRhythm 文生图（OpenAI 兼容协议，免费调用 qwen-image-2.0 / wan2.7-image）
# 文档：https://tokenrhythm.studio/docs/api-integration
# 鉴权：请求头 Authorization: Bearer sk_xxx，base_url 默认 https://tokenrhythm.studio/v1
# ========================================================================

TOKENRHYTHM_API_KEY  = os.getenv("TOKENRHYTHM_API_KEY", "")
TOKENRHYTHM_BASE_URL = os.getenv("TOKENRHYTHM_BASE_URL", "https://tokenrhythm.studio/v1")

# ========================================================================
# 百炼 LLM（OpenAI 兼容协议，复用百炼 IMAGE_API_KEY / TTS_API_KEY）
# ========================================================================

BAILIAN_LLM_BASE_URL = os.getenv("BAILIAN_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

# ========================================================================
# LLM 模型路由（设置页可切换每个调用点使用的模型）
# 五个候选：百炼 Qwen3.7-Flash（默认/兜底）/ 百炼 Qwen3.7-Max（限时5折）
#          / 百炼 Qwen3.7-Max 0520 快照 / 百炼 DeepSeek-V4-Flash / DeepSeek 官方
# value 为内部标识；base_url/api_key/model 为该模型实际调用参数。
# ========================================================================

LLM_MODELS = [
    {
        "value": "bailian-qwen3.7-flash",
        "label": "百炼 · Qwen3.7 Flash（默认/性价比最高）",
        "channel": "bailian",
        "base_url": BAILIAN_LLM_BASE_URL,
        "api_key": IMAGE_API_KEY,
        "model": "qwen3.7-flash-2026-07-15",
        "tier": "默认",
        "price": "输入 0.2 / 输出 0.8 元(每百万token)",
        "note": "千问3.7 Flash（2026-07-15），支持 JSON 结构化输出；免费额度 100 万 token（华北2）；全站默认与兜底降级模型",
        "recommended": True,
    },
    {
        "value": "bailian-qwen3.7-max",
        "label": "百炼 · Qwen3.7 Max（限时5折）",
        "channel": "bailian",
        "base_url": BAILIAN_LLM_BASE_URL,
        "api_key": IMAGE_API_KEY,
        "model": "qwen3.7-max",
        "tier": "限时",
        "price": "限时5折 输入 6 / 输出 18 元(每百万token)",
        "note": "千问3.7 Max 旗舰（能力等同 2026-05-20 快照），限时 5 折；免费额度 100 万 token（8.20 到期）",
        "recommended": False,
    },
    {
        "value": "bailian-qwen3.7-max-2026-05-20",
        "label": "百炼 · Qwen3.7 Max 0520（旗舰快照）",
        "channel": "bailian",
        "base_url": BAILIAN_LLM_BASE_URL,
        "api_key": IMAGE_API_KEY,
        "model": "qwen3.7-max-2026-05-20",
        "tier": "旗舰",
        "price": "输入 12 / 输出 36 元(每百万token)",
        "note": "千问3.7 Max 2026-05-20 快照版（固定版本）；免费额度 100 万 token（8.20 到期）",
        "recommended": False,
    },
    {
        "value": "bailian-deepseek-v4-flash",
        "label": "百炼 · DeepSeek V4 Flash",
        "channel": "bailian",
        "base_url": BAILIAN_LLM_BASE_URL,
        "api_key": IMAGE_API_KEY,
        "model": "deepseek-v4-flash",
        "tier": "旧价",
        "price": "输入 1 / 输出 2 元(每百万token)",
        "note": "百炼渠道 DeepSeek-V4-Flash（旧版快照价），英文文本能力强；注意 8/17 后是否并价",
        "recommended": False,
    },
    {
        "value": "deepseek-official",
        "label": "DeepSeek 官方 · V4 Flash",
        "channel": "deepseek",
        "base_url": DEEPSEEK_BASE,
        "api_key": DEEPSEEK_API_KEY,
        "model": DEEPSEEK_MODEL,
        "tier": "官方",
        "price": "涨后 闲时输入 1.5 / 输出 4.5 · 高峰 3 / 9 元(每百万token)",
        "note": "官方直连，2026-08-17 起峰谷计费（高峰 9-12、14-18 点）",
        "recommended": False,
    },
]

# 每个 LLM 调用点（设置页可独立选择模型）。default 为该调用点的默认模型 value，
# 同时也是该调用点选定模型失败时的兜底降级模型（默认与兜底统一为百炼 Qwen3.7-Flash）。
LLM_ROUTES = [
    {"key": "batch",  "label": "批量编译",      "desc": "剧情连环画生成（荒诞/冲突/场景/微电影）", "default": "bailian-qwen3.7-flash"},
    {"key": "single", "label": "单点深耕",      "desc": "单词语义记忆卡片生成", "default": "bailian-qwen3.7-flash"},
    {"key": "video",  "label": "视频脚本",      "desc": "视频编译的旁白与提示词", "default": "bailian-qwen3.7-flash"},
    {"key": "polysemy", "label": "熟词僻意检测", "desc": "批量判断是否为托业高频熟词僻意", "default": "bailian-qwen3.7-flash"},
    {"key": "enrich", "label": "单词补充",       "desc": "词性/释义自动补全（失败降级默认模型）", "default": "bailian-qwen3.7-flash"},
    {"key": "scene_detect", "label": "场景检测", "desc": "单词自动归类到场景", "default": "bailian-qwen3.7-flash"},
    {"key": "scene_collocations", "label": "场景词伙", "desc": "场景内词伙搭配生成", "default": "bailian-qwen3.7-flash"},
]

LLM_ROUTE_DEFAULT = {r["key"]: r["default"] for r in LLM_ROUTES}
LLM_MODEL_BY_VALUE = {m["value"]: m for m in LLM_MODELS}