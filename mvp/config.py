"""
WordIsle MVP 配置
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

# 模型 → 默认推荐音色。模型与音色必须同系列，避免不兼容（Qwen-Audio-TTS 系列用 longanhuan_*，
# CosyVoice 系列用 loongandy/loongbeth/loongemily/loongeric）。切换模型时音色应随之切换。
TTS_DEFAULT_VOICE = {
    "qwen-audio-3.0-tts-plus": "longanhuan_v3.6",
    "cosyvoice-v3-flash":      "loongandy_v3",
}

# 可选 TTS 模型清单（与前端 constants.js 的 TTS_MODELS 对齐，用于设置页校验/单词发音）。
# 仅收录经实测可用的模型：cosyvoice-v3-plus 当前 TTS 通道下不可用（engine 418），故未收录。
TTS_MODEL_VALUES = ["qwen-audio-3.0-tts-plus", "cosyvoice-v3-flash"]

# 各 TTS 模型可选音色清单（单词发音设置页"音色"下拉，与模型同系列，均为实测可用）
TTS_MODEL_VOICES = {
    "qwen-audio-3.0-tts-plus": ["longanhuan_v3.6"],
    "cosyvoice-v3-flash":      ["loongandy_v3", "loongbeth_v3", "loongemily_v3", "loongeric_v3"],
}

# 音色说明（前端设置页音色下拉展示用）
TTS_VOICE_NOTES = {
    "loongandy_v3":  "美式男 · 自然清晰",
    "loongbeth_v3":  "美式女 · 温柔甜美",
    "loongemily_v3": "英式女 · 优雅标准",
    "loongeric_v3":  "英式男 · 沉稳专业",
    "longanhuan_v3.6": "系统默认 · 支持多语种",
}


def default_tts_voice(model: str) -> str:
    """返回某 TTS 模型的默认音色；未知名则回退到全局默认音色。"""
    return TTS_DEFAULT_VOICE.get(model, TTS_VOICE)


def normalize_tts_voice(model: str, voice: str = "") -> str:
    """把音色收敛到该模型支持的集合：非法/跨系列音色回退默认音色，避免上游 500。"""
    allowed = set(TTS_MODEL_VOICES.get(model, [])) | {default_tts_voice(model)}
    return voice if voice in allowed else default_tts_voice(model)

# ========================================================================
# 文生图（阿里云百炼，复用 TTS_API_KEY）
# ========================================================================

IMAGE_API_KEY  = os.getenv("IMAGE_API_KEY", TTS_API_KEY)
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")
IMAGE_MODEL    = os.getenv("IMAGE_MODEL", "qwen-image-2.1-turbo")

# 文生图模型两档（用户可在前端自由选择，价格依据阿里云百炼图像生成价格页）
# 各档 endpoint：
#   multimodal -> 千问图像系列（同步 generation 端点）
#   t2i        -> 万相文生图（异步轮询 image-synthesis 端点，当前图像档未使用）
IMAGE_MODELS = [
    # ============ 性价比档（默认推荐） ============
    {
        "value": "qwen-image-2.1-turbo",
        "label": "性价比 · Qwen-Image 2.1 Turbo (默认推荐)",
        "tier": "性价比",
        "price": "0.10 元/张",
        "note": "8 步去噪的 7B 模型，原生 2K，支持透明底/局部编辑/最多10张参考图；单价 0.10 元，日常插画首选",
        "features": "0.10元/张；原生2K；8步快速出图；透明底；最多10张参考图；支持局部编辑",
        "scenarios": "批量编译、单点深耕、场景编译的默认选择",
        "endpoint": "multimodal",
        "size": "1944*1032",
        "prompt_extend": True,
    },
    # ============ 质量档（画面内文字/复杂版面） ============
    {
        "value": "qwen-image-3.0",
        "label": "质量 · Qwen-Image 3.0 (文字/复杂版面)",
        "tier": "质量",
        "price": "0.18 元/张",
        "note": "画面内需要出现准确英文单词、复杂版面时选它；1K/2K 同价 0.18 元",
        "features": "文字渲染强；复杂版面；1K/2K 同价",
        "scenarios": "画面需出现英文单词、海报式构图时",
        "endpoint": "multimodal",
        "size": "1944*1032",
        "prompt_extend": True,
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
# 两档：Qwen3.7-Flash（默认/兜底，0.2/0.8）+ Qwen3.8-Flash（创作质量档，0.8/2.7）
# value 为内部标识；base_url/api_key/model 为该模型实际调用参数。
# ========================================================================

LLM_MODELS = [
    {
        "value": "bailian-qwen3.7-flash",
        "label": "百炼 · Qwen3.7 Flash（默认/性价比最高）",
        "channel": "bailian",
        "base_url": BAILIAN_LLM_BASE_URL,
        "api_key": IMAGE_API_KEY,
        "model": "qwen3.7-flash",
        "tier": "默认",
        "price": "输入 0.2 / 输出 0.8 元(每百万token，≤32K)",
        "note": "别名当前等同于 qwen3.7-flash-2026-07-15，但别名有 100 万免费额度（快照无）；≤32K 按 0.2/0.8，32K-256K 为 0.6/2.4；全站默认与兜底降级模型",
        "recommended": True,
    },
    {
        "value": "bailian-qwen3.8-flash",
        "label": "百炼 · Qwen3.8 Flash（创作质量档）",
        "channel": "bailian",
        "base_url": BAILIAN_LLM_BASE_URL,
        "api_key": IMAGE_API_KEY,
        "model": "qwen3.8-flash",
        "tier": "质量",
        "price": "输入 0.8 / 输出 2.7 元(每百万token)",
        "note": "新一代 Flash，文笔与指令遵循更好；比 3.7 贵约 4 倍，建议只给批量编译/单点深耕；100 万免费额度",
        "recommended": False,
    },
]

# 每个 LLM 调用点（设置页可独立选择模型）。default 为该调用点的默认模型 value，
# 同时也是该调用点选定模型失败时的兜底降级模型（默认与兜底统一为百炼 Qwen3.7-Flash）。
LLM_ROUTES = [
    {"key": "batch",  "label": "批量编译",      "desc": "剧情连环画生成（荒诞/冲突/场景/微电影）", "default": "bailian-qwen3.7-flash"},
    {"key": "single", "label": "单点深耕",      "desc": "单词语义记忆卡片生成", "default": "bailian-qwen3.7-flash"},
    {"key": "video",  "label": "视频脚本",      "desc": "视频编译的旁白与提示词", "default": "bailian-qwen3.7-flash"},
    {"key": "polysemy", "label": "熟词僻意检测", "desc": "批量判断是否为高频熟词僻意（含商务义）", "default": "bailian-qwen3.7-flash"},
    {"key": "enrich", "label": "单词补充",       "desc": "词性/释义自动补全（失败降级默认模型）", "default": "bailian-qwen3.7-flash"},
    {"key": "scene_detect", "label": "场景检测", "desc": "单词自动归类到场景", "default": "bailian-qwen3.7-flash"},
    {"key": "scene_collocations", "label": "场景词伙", "desc": "场景内词伙搭配生成", "default": "bailian-qwen3.7-flash"},
    {"key": "morpheme", "label": "构词拆解",     "desc": "单词构词拆解判定 / 词根推荐词生成", "default": "bailian-qwen3.7-flash"},
    {"key": "morpheme_seed", "label": "构词拆解·词根推荐", "desc": "为词根树推荐同构词（懒填充/添加成员）", "default": "bailian-qwen3.7-flash"},
    {"key": "extract", "label": "文章提词", "desc": "从粘贴的文章/文档中提取值得学习的单词", "default": "bailian-qwen3.7-flash"},
    {"key": "assistant", "label": "智能助手词小屿", "desc": "词小屿意图识别与回复生成（设置页可切换模型）", "default": "bailian-qwen3.7-flash"},
]

LLM_ROUTE_DEFAULT = {r["key"]: r["default"] for r in LLM_ROUTES}
LLM_MODEL_BY_VALUE = {m["value"]: m for m in LLM_MODELS}

# ========================================================================
# 构词拆解：内置常见词缀/词根表（启发式粗筛，命中才进 LLM 确认名单，省 token）
# ========================================================================

COMMON_MORPHEMES = {
    "suffixes": ["-age", "-ment", "-tion", "-sion", "-ness", "-able", "-ible", "-ance", "-ence", "-ity",
                 "-ful", "-less", "-ly", "-er", "-or", "-ist", "-ship", "-hood", "-ure", "-al", "-ive", "-ous"],
    "prefixes": ["re-", "un-", "in-", "im-", "ir-", "il-", "dis-", "pre-", "pro-", "sub-", "trans-",
                 "inter-", "counter-", "mis-", "over-", "under-", "de-", "ex-", "en-", "em-"],
    "roots": ["ag/act", "cred", "struct", "port", "dict", "scribe/script", "audi", "loc", "vac",
              "spect", "rupt", "ject", "mit/miss", "duct", "form", "press", "tract", "sign", "vid/vis"],
}

# 启发式匹配帮助函数：判断某词是否命中任意构词构件（供扫描粗筛用）
def hit_common_morpheme(word: str) -> bool:
    w = word.lower()
    for suf in COMMON_MORPHEMES["suffixes"]:
        if suf[1:] and w.endswith(suf[1:]):  # "-age" → 匹配尾部 "age"
            return True
    for pre in COMMON_MORPHEMES["prefixes"]:
        if pre[:-1] and w.startswith(pre[:-1]):  # "re-" → 匹配头部 "re"
            return True
    return False


# ========================================================================
# 用户系统（认证 + 每用户分库 + 每日配额）
# ========================================================================

# 会话签名密钥（.env 必配；本地开发留空时使用内置降级密钥）
AUTH_SECRET = os.getenv("AUTH_SECRET", "dev-insecure-secret-change-me")

# 每用户独立数据库目录（业务库按 uid 分文件）与全局库（users / quotas）
USER_DATA_DIR = DATA_DIR / "user"
SYSTEM_DB_PATH = DATA_DIR / "system.db"

# 开发者账号：沿用原 Nginx Basic Auth 账号，密码从 .env 读取（不落明文文档）
DEV_USERNAME = os.getenv("DEV_USERNAME", "dev")
DEV_PASSWORD  = os.getenv("DEV_PASSWORD", "")

# 管理员固定账号：ADMIN_USERS=admin1:pass1,admin2:pass2（逗号分隔 username:password）
ADMIN_USERS: list[tuple[str, str]] = []
for _pair in os.getenv("ADMIN_USERS", "").split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _u, _p = _pair.split(":", 1)
        ADMIN_USERS.append((_u.strip(), _p.strip()))

# 游客每日配额：bucket 名与 LLM_ROUTES 键保持一致，避免两套命名
#   video 视频编译 2 次/日；其余内容编译类（batch/single/scene/polysemy/morpheme/extract/enrich）各 5 次/日
GUEST_LIMITS = {
    "video": 2, "batch": 5, "single": 5, "scene": 5,
    "polysemy": 5, "morpheme": 5, "extract": 5, "enrich": 5,
    "tts": 20,        # 单词/文本发音合成（缓存命中不计次）
    "assistant": 50,  # 词小屿每日对话上限（FAQ 命中与 LLM 回复均计入；dev/admin 不限）
}
# bucket → 中文名（用于前端提示 / 后端 429 文案）
QUOTA_BUCKET_LABELS = {
    "video": "视频编译", "batch": "批量编译", "single": "单点深耕", "scene": "场景检测/编译",
    "polysemy": "熟词僻意检测", "morpheme": "构词拆解", "extract": "文章提词", "enrich": "单词补充",
    "tts": "语音合成", "assistant": "智能助手对话",
}

# 每日复习量上限（防堆积，Anki 式，routes 与词小屿共用）
REVIEW_DAILY_LIMIT = 20

# 会话 Cookie
AUTH_COOKIE = "wordisle_session"
AUTH_MAX_AGE = 30 * 24 * 3600  # 30 天
# 上 HTTPS 后设为 1：Cookie 加 Secure，只走加密连接（HTTP 下必须为 0，否则登录态写不进去）
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"

# 本地开发/回归测试可整体关闭认证（中间件放行、/api/me 返回默认开发者身份）
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "") == "1"

# ========================================================================
# 注册用户余额计费（按使用额度付费）
#   - 游客：每日配额（GUEST_LIMITS）
#   - 注册用户 role=user：账户余额制，每次生成按 bucket 单价扣减，不足 402
#   - dev/admin：无限量
# ========================================================================

# bucket → 单价（岛屿币/次）。VIDEO 等重操作单价高
BUCKET_PRICES = {
    "video": 10, "scene": 5, "batch": 2, "single": 2,
    "polysemy": 1, "morpheme": 1, "extract": 1, "enrich": 1, "tts": 1, "assistant": 1,
}

# 新用户注册赠送 / 游客升级追加赠送（岛屿币）
REGISTER_GIFT = 50
UPGRADE_GIFT = 50

# ========================================================================
# 手机号注册 - 图形验证码（SVG，零依赖）+ 阿里云短信
# ========================================================================

# 图形验证码：内存态，一次性，300 秒过期，尝试 5 次作废
CAPTCHA_TTL = 300
CAPTCHA_MAX_TRIES = 5

# 短信验证码：6 位数字，SHA256 存储，5 分钟有效，尝试 5 次作废
SMS_CODE_TTL = 300
SMS_MAX_TRIES = 5
SMS_COOLDOWN_SECONDS = 60          # 同手机重发冷却
SMS_DAILY_PER_PHONE = 5            # 同手机每日发送上限
SMS_DAILY_PER_IP = 10              # 同 IP 每日发送上限

# 阿里云短信（Dysmsapi）。未配置 KEY 时自动降级「测试模式」：验证码固定 123456 + 日志打码
ALIYUN_SMS_ACCESS_KEY_ID = os.getenv("ALIYUN_SMS_ACCESS_KEY_ID", "")
ALIYUN_SMS_ACCESS_KEY_SECRET = os.getenv("ALIYUN_SMS_ACCESS_KEY_SECRET", "")
ALIYUN_SMS_SIGN_NAME = os.getenv("ALIYUN_SMS_SIGN_NAME", "")
ALIYUN_SMS_TEMPLATE_CODE = os.getenv("ALIYUN_SMS_TEMPLATE_CODE", "")

# 短信验证码允许的用途类型（发送时校验，防传任意值）
SMS_SEND_TYPES = ("register", "guest_upgrade", "reset", "rebind")

# ========================================================================
# 登录安全：失败递进锁定（账号级 + IP 级双轨，落库可审计）
# ========================================================================

# 达到累计失败次数 → 锁定时长（秒）。5 次锁 5 分钟 → 10 次锁 30 分 → 20 次锁 2 小时
LOGIN_LOCK_THRESHOLDS = [(5, 300), (10, 1800), (20, 7200)]

# ========================================================================
# 防刷与限流（单进程内存态；重启清零）
# ========================================================================

# 游客创建：单 IP 每日上限 + 全站每日上限（后者是成本熔断，防止换 IP 无限刷）
GUEST_DAILY_PER_IP = int(os.getenv("GUEST_DAILY_PER_IP", "30"))
GUEST_DAILY_TOTAL = int(os.getenv("GUEST_DAILY_TOTAL", "500"))

# HTTP 限流：单 IP 每分钟请求数（仅 /api/，本地回环与测试客户端豁免）
# 注意：nginx 已在网关层做 30r/s 限流，这里是应用层兜底，阈值放宽避免 NAT 后多用户误伤
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "1200"))
# 验证码生成是 CPU 密集操作，单独收紧
RATE_LIMIT_CAPTCHA_PER_MINUTE = int(os.getenv("RATE_LIMIT_CAPTCHA_PER_MINUTE", "20"))

# 跨域来源（默认空 = 只允许同源；需要跨域调用时用逗号分隔配置）
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

# 输入体积上限（防单请求烧钱 / 占内存）
MAX_WORDS_PER_IMPORT = int(os.getenv("MAX_WORDS_PER_IMPORT", "200"))
MAX_EXTRACT_TEXT_CHARS = int(os.getenv("MAX_EXTRACT_TEXT_CHARS", "20000"))
MAX_ASSISTANT_MESSAGE_CHARS = int(os.getenv("MAX_ASSISTANT_MESSAGE_CHARS", "4000"))
# 批量导入时最多预生成多少个单词发音（其余按需懒生成）
MAX_PREFETCH_AUDIO = int(os.getenv("MAX_PREFETCH_AUDIO", "10"))

# ========================================================================
# 成本熔断
# ========================================================================

# 视频生成默认关闭（单价高、当前产品未启用）；需要时显式 VIDEO_ENABLED=1
VIDEO_ENABLED = os.getenv("VIDEO_ENABLED", "0") == "1"

# 全站每日文生图张数上限（所有用户合计，单进程内存态；重启清零）。
# 按 0.10 元/张估算，300 张 ≈ 30 元/日的硬上限，防止被刷爆。
DAILY_IMAGE_LIMIT = int(os.getenv("DAILY_IMAGE_LIMIT", "300"))