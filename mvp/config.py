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
TEMPLATES_DIR = BASE_DIR / "templates"

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
        "endpoint": "t2i",
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
        "endpoint": "openai",
        "provider": "tokenrhythm",
        "size": "1024x1024",
    },
]

# ========================================================================
# TokenRhythm 文生图（OpenAI 兼容协议，免费调用 qwen-image-2.0 / wan2.7-image）
# 文档：https://tokenrhythm.studio/docs/api-integration
# 鉴权：请求头 Authorization: Bearer sk_xxx，base_url 默认 https://tokenrhythm.studio/v1
# ========================================================================

TOKENRHYTHM_API_KEY  = os.getenv("TOKENRHYTHM_API_KEY", "")
TOKENRHYTHM_BASE_URL = os.getenv("TOKENRHYTHM_BASE_URL", "https://tokenrhythm.studio/v1")

# ========================================================================
# 廉价 LLM（用于简单任务，如单词补充、熟词检测；先试廉价模型，失败降级到 DeepSeek）
# ========================================================================

CHEAP_LLM_API_KEY  = os.getenv("CHEAP_LLM_API_KEY", "")
CHEAP_LLM_BASE_URL = os.getenv("CHEAP_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
CHEAP_LLM_MODEL    = os.getenv("CHEAP_LLM_MODEL", "glm-4.7-flash")

# ========================================================================
# 每日限额
# ========================================================================

DAILY_AI_LIMIT    = int(os.getenv("DAILY_AI_LIMIT", "20"))
DAILY_TTS_LIMIT   = int(os.getenv("DAILY_TTS_LIMIT", "50"))
DAILY_IMAGE_LIMIT = int(os.getenv("DAILY_IMAGE_LIMIT", "50"))