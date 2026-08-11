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

# 文生图模型三档（用户可在前端自由选择）
IMAGE_MODELS = [
    {
        "value": "qwen-image-3.0-pro",
        "label": "旗舰 · Qwen-Image 3.0 Pro (画质最佳·文本渲染强)",
        "tier": "旗舰",
        "price": "0.50 元/张",
        "note": "千问3.0旗舰版，支持agent prompt智能改写，擅长中英文文本渲染；复杂版面首选",
        "endpoint": "multimodal",
    },
    {
        "value": "wan2.7-image",
        "label": "均衡 · Wan 2.7 Image (角色一致·适合连环画)",
        "tier": "均衡",
        "price": "0.20 元/张",
        "note": "角色一致性多图生成，连环画人物统一；50张免费额度，2K分辨率",
        "endpoint": "t2i",
    },
    {
        "value": "z-image-turbo",
        "label": "性价比 · Z-Image Turbo (最快·约0.04元/张)",
        "tier": "性价比",
        "price": "0.04 元/张",
        "note": "快速低成本，速度比wan2.7快10倍；写实人像和产品照片；仅文生图不支持编辑",
        "endpoint": "multimodal",
    },
]

# ========================================================================
# 每日限额
# ========================================================================

DAILY_AI_LIMIT    = int(os.getenv("DAILY_AI_LIMIT", "20"))
DAILY_TTS_LIMIT   = int(os.getenv("DAILY_TTS_LIMIT", "50"))
DAILY_IMAGE_LIMIT = int(os.getenv("DAILY_IMAGE_LIMIT", "50"))