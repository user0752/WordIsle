"""滑块拼图图形验证码（Pillow 位图渲染，极验/腾讯云交互范式）。

- 主流程（两阶段，与真实滑块验证码一致）：
    1. `POST /api/captcha`            → 下发 captcha_id + 带缺口背景 + 拼图块（不包含答案）
    2. 用户拖动松手 → `/api/captcha/verify`（verify_release 校验，通过则标记 verified，
       不消费，可同图重试若干次）→ 前端展示「通过」反馈
    3. 真正消费发生在 `/api/sms/send`（verify_captcha：要求 verified 且坐标容差内，
       一次性删除，无法绕过图形验证直接发短信）
- 视觉：程序化生成「照片感」风景背景（天空渐变 / 日月光晕 / 云层 / 远山近丘），
  缺口做凹陷内阴影、拼图块圆角 + 描边 + CSS drop-shadow，观感贴近主流网页验证码。
- 位图而非 SVG：矢量缺口坐标可被脚本直接解析，位图只能被 CV 定位。
- 答案=目标 x 坐标（服务端内存态存储，绝不下发前端）。
"""
import base64
import io
import math
import random
import secrets
import threading
import time

from PIL import Image, ImageDraw, ImageFilter

from config import CAPTCHA_MAX_TRIES, CAPTCHA_TTL

# secrets 无 randrange（那是 random 的），用 SystemRandom 做加密安全随机
_rand = random.SystemRandom()

# 验证码存储：{captcha_id: {"target": int|None, "created_at": float, "try_count": int, "verified": bool}}
_captcha_store: dict[str, dict] = {}
_store_lock = threading.Lock()

# 防止内存被刷爆：未过期待校验验证码上限，超限时淘汰最旧一条
CAPTCHA_MAX_PENDING = 10000

# 画布与拼图参数（px）
CAPTCHA_W = 300
CAPTCHA_H = 100
PIECE_SIZE = 56                                  # 拼图块边长（方形）
PIECE_Y = (CAPTCHA_H - PIECE_SIZE) // 2          # 垂直居中放置
TOLERANCE = 12                                   # x 偏差容差（px）


# ---------------------------------------------------------------------------
# 程序化「照片感」背景
# ---------------------------------------------------------------------------
# 每套主题：{name, sky: [(pos, color)…]（垂直渐变），glow: 光晕色 RGBA 或 None，
#           scene: 'hills'|'lake'|'night'}。颜色均取低饱和自然系，避免廉价感。
_THEMES = [
    {
        "name": "morning",
        "desc": "晨雾青绿",
        "sky": [(0.0, (104, 156, 176)), (0.55, (178, 212, 200)), (1.0, (235, 240, 222))],
        "glow": (255, 244, 214), "scene": "hills",
    },
    {
        "name": "sunset",
        "desc": "日落暖橙",
        "sky": [(0.0, (62, 64, 128)), (0.42, (156, 96, 138)), (0.72, (232, 132, 92)), (1.0, (252, 206, 134))],
        "glow": (255, 226, 168), "scene": "hills",
    },
    {
        "name": "blue",
        "desc": "晴空蓝天",
        "sky": [(0.0, (52, 108, 186)), (0.55, (128, 186, 228)), (0.88, (214, 232, 240)), (1.0, (242, 244, 230))],
        "glow": (252, 248, 214), "scene": "hills",
    },
    {
        "name": "night",
        "desc": "静谧夜空",
        "sky": [(0.0, (10, 14, 40)), (0.55, (26, 34, 70)), (1.0, (52, 52, 92))],
        "glow": (235, 238, 255), "scene": "night",
    },
    {
        "name": "lake",
        "desc": "湖畔清波",
        "sky": [(0.0, (70, 148, 208)), (0.58, (148, 204, 234)), (1.0, (196, 222, 236))],
        "glow": (250, 250, 240), "scene": "lake",
    },
]


def _lerp(a, b, t):
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _color_at(sky, t):
    """按位置取渐变颜色（线性插值分段）。"""
    for i in range(len(sky) - 1):
        p0, c0 = sky[i]
        p1, c1 = sky[i + 1]
        if p0 <= t <= p1:
            return _lerp(c0, c1, (t - p0) / max(1e-6, p1 - p0))
    return sky[-1][1]


def _ridge_points(base_y, amp, phase, step=8):
    """波浪形山脊折线（sin 缓坡 + 随机起伏），返回 [(x, y)…]。"""
    pts = []
    for x in range(0, CAPTCHA_W + step, step):
        wave = 0.5 + 0.5 * math.sin(x / 24.0 + phase)
        y = base_y - amp * (0.35 + 0.65 * wave) + _rand.randrange(-3, 4)
        pts.append((x, y))
    return pts


def _rand_background() -> Image.Image:
    """生成随机「照片感」位图背景（天空渐变 + 光晕 + 云 + 山丘/湖面 + 噪点 + 暗角）。"""
    theme = secrets.choice(_THEMES)
    img = Image.new("RGB", (CAPTCHA_W, CAPTCHA_H))
    draw = ImageDraw.Draw(img)

    # 1) 天空垂直渐变
    for y in range(CAPTCHA_H):
        t = y / (CAPTCHA_H - 1)
        draw.line([(0, y), (CAPTCHA_W, y)], fill=_color_at(theme["sky"], t))

    # 2) 光晕层（太阳/月亮，径向渐隐）+ 云层，统一 RGBA 叠加
    overlay = Image.new("RGBA", (CAPTCHA_W, CAPTCHA_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    glow = theme["glow"]
    gx = _rand.randrange(40, CAPTCHA_W - 40)
    gy = _rand.randrange(16, 46)
    gr = _rand.randrange(16, 26)
    for i in range(10, 0, -1):
        r = int(gr * (11 - i) / 10)
        od.ellipse([gx - r, gy - r, gx + r, gy + r], fill=(*glow, int(58 * (i / 10) ** 2)))
    od.ellipse([gx - int(gr * 0.55), gy - int(gr * 0.55), gx + int(gr * 0.55), gy + int(gr * 0.55)],
               fill=(*glow, 230))

    # 3) 云（仅在白天系主题；夜空主题跳过）
    if theme["scene"] != "night":
        for _ in range(_rand.randrange(2, 5)):
            cx = _rand.randrange(10, CAPTCHA_W - 10)
            cy = _rand.randrange(6, 42)
            r = _rand.randrange(14, 30)
            alpha = _rand.randrange(60, 120)
            od.ellipse([cx - r, cy - r * 0.55, cx + r, cy + r * 0.55], fill=(255, 255, 255, alpha))
            od.ellipse([cx - r * 0.5, cy - r * 0.35, cx + r * 1.4, cy + r * 0.75], fill=(255, 255, 255, alpha - 25))
            od.ellipse([cx - r * 1.1, cy - r * 0.15, cx + r * 0.35, cy + r * 0.5], fill=(255, 255, 255, alpha - 30))
        overlay = overlay.filter(ImageFilter.GaussianBlur(2.2))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # 4) 远山 → 近丘（越近越深，晚主题额外带星光）
    draw = ImageDraw.Draw(img)
    draw.polygon([(0, CAPTCHA_H)] + _ridge_points(CAPTCHA_H - 6, 16, _rand.random() * 6) + [(CAPTCHA_W, CAPTCHA_H)],
                 fill=_color_at(theme["sky"], 0.98))
    if theme["scene"] == "lake":
        # 湖面：下三分之一，上面反射天空光，带横向波光
        water_top = CAPTCHA_H - 26
        base = _color_at(theme["sky"], 0.85)
        water = _lerp(base, (255, 255, 255), 0.35)
        draw.rectangle([0, water_top, CAPTCHA_W, CAPTCHA_H], fill=water)
        for _ in range(_rand.randrange(14, 24)):
            ly = _rand.randrange(water_top + 2, CAPTCHA_H - 2)
            lx = _rand.randrange(0, CAPTCHA_W - 30)
            lw = _rand.randrange(12, 30)
            draw.line([(lx, ly), (lx + lw, ly)], fill=(255, 255, 255), width=1)
        near = _lerp(base, (60, 80, 96), 0.55)
    elif theme["scene"] == "night":
        # 星光
        for _ in range(26):
            sx, sy = _rand.randrange(0, CAPTCHA_W), _rand.randrange(0, 52)
            draw.point((sx, sy), fill=_rand.choice([(255, 255, 255), (235, 240, 255), (255, 245, 220), (210, 222, 255)]))
        near = (34, 40, 66)
    else:
        near = _color_at(theme["sky"], 0.97)
    draw.polygon([(0, CAPTCHA_H)] + _ridge_points(CAPTCHA_H - 3, 22, _rand.random() * 6 + 3) + [(CAPTCHA_W, CAPTCHA_H)],
                 fill=near)

    # 5) 细节：噪点 + 暗角 + 底部微光
    for _ in range(50):
        nx, ny = _rand.randrange(0, CAPTCHA_W), _rand.randrange(0, CAPTCHA_H)
        c = img.getpixel((nx, ny))
        delta = _rand.randrange(-14, 15)
        img.putpixel((nx, ny), tuple(max(0, min(255, v + delta)) for v in c))
    vign = Image.new("L", (CAPTCHA_W, CAPTCHA_H), 0)
    vd = ImageDraw.Draw(vign)
    for i in range(16):
        a = int(70 * (1 - i / 16) ** 2)
        vd.rectangle([i, i, CAPTCHA_W - 1 - i, CAPTCHA_H - 1 - i], outline=a)
    # composite(a, b, mask)：mask=0 → b，mask=255 → a；故黑底在前、原图在后，边缘按 mask 压暗
    img = Image.composite(Image.new("RGB", img.size, (0, 0, 0)), img, vign)
    return img


# ---------------------------------------------------------------------------
# 拼图块与缺口（腾讯/极验风格）
# ---------------------------------------------------------------------------

def _make_piece_and_hole(bg: Image.Image, target_x: int) -> tuple[Image.Image, Image.Image]:
    """生成 {拼图块(RGBA 圆角带描边), 缺口背景(凹陷) }，均在 target_x 位置。"""
    region = bg.crop((target_x, PIECE_Y, target_x + PIECE_SIZE, PIECE_Y + PIECE_SIZE)).convert("RGBA")

    # ---- 缺口背景：原图变暗作「半透明玻璃感」+ 内阴影（上/左暗、下/右亮） + 外描边 ----
    hole = bg.copy().convert("RGBA")
    gray = region.convert("L")
    dim = Image.merge(
        "RGB",
        [gray.point(lambda p: min(255, int(p * 0.5)))] * 3,
    )
    hole.paste(dim, (target_x, PIECE_Y))
    hd = ImageDraw.Draw(hole)
    r = 8
    for i in range(6):
        a = int(56 * (1 - i / 6))
        hd.line([(target_x + r + i, PIECE_Y + r + i), (target_x + PIECE_SIZE - r - i, PIECE_Y + r + i)],
                fill=(0, 0, 0, a))
        hd.line([(target_x + r + i, PIECE_Y + r + i), (target_x + r + i, PIECE_Y + PIECE_SIZE - r - i)],
                fill=(0, 0, 0, int(a * 0.6)))
        hd.line([(target_x + r + i, PIECE_Y + PIECE_SIZE - r - i), (target_x + PIECE_SIZE - r - i, PIECE_Y + PIECE_SIZE - r - i)],
                fill=(255, 255, 255, int(a * 0.5)))
        hd.line([(target_x + PIECE_SIZE - r - i, PIECE_Y + r + i), (target_x + PIECE_SIZE - r - i, PIECE_Y + PIECE_SIZE - r - i)],
                fill=(255, 255, 255, int(a * 0.35)))
    hd.rounded_rectangle(
        [target_x, PIECE_Y, target_x + PIECE_SIZE, PIECE_Y + PIECE_SIZE],
        radius=r, outline=(0, 0, 0, 60), width=1,
    )

    # ---- 拼图块：圆角 + 内侧立体描边（左上亮 / 右下暗） ----
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, PIECE_SIZE, PIECE_SIZE], radius=r, fill=255)
    piece = region.copy()
    piece.putalpha(mask)
    pd = ImageDraw.Draw(piece)
    pd.rounded_rectangle([0, 0, PIECE_SIZE - 1, PIECE_SIZE - 1], radius=r, outline=(255, 255, 255, 90), width=1)
    pd.rounded_rectangle([2, 2, PIECE_SIZE - 3, PIECE_SIZE - 3], radius=r - 1, outline=(0, 0, 0, 55), width=2)
    pd.rounded_rectangle([4, 4, PIECE_SIZE - 5, PIECE_SIZE - 5], radius=r - 2, outline=(255, 255, 255, 34), width=1)

    return piece, hole


def _img_to_data_uri(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/{fmt.lower()};base64,{b64}"


# ---------------------------------------------------------------------------
# 两阶段校验：verify_release（松手校验，标记 verified） / verify_captcha（消费）
# ---------------------------------------------------------------------------

def new_captcha_id(answer: str) -> str:
    """生成新验证码：存储目标 x 坐标（服务端内存态，不下发前端），返回 captcha_id。"""
    captcha_id = secrets.token_hex(16)
    try:
        target = int(str(answer).strip())
    except (TypeError, ValueError):
        target = None   # 非法坐标：生成的验证码永远校验不过（测试契约要求坐标字符串）
    with _store_lock:
        _purge_expired_locked()
        # 超限保护：未过期待校验验证码超过上限时，依次淘汰最旧的
        while len(_captcha_store) >= CAPTCHA_MAX_PENDING:
            oldest = next(iter(_captcha_store))
            _captcha_store.pop(oldest, None)
        _captcha_store[captcha_id] = {
            "target": target,
            "created_at": time.time(),
            "try_count": 0,
            "verified": False,
        }
    return captcha_id


def _purge_expired_locked():
    """清理过期项（须持有 _store_lock 时调用）。"""
    now = time.time()
    dead = [cid for cid, v in _captcha_store.items() if now - v["created_at"] > CAPTCHA_TTL]
    for cid in dead:
        _captcha_store.pop(cid, None)


def verify_release(captcha_id: str, x: str) -> bool:
    """滑块松手校验（弹窗拖动后的即时反馈）：坐标容差内 → 标记 verified（不消费，可同图重试）。
    失败累积 try_count，超过上限作废；返回是否通过。答案仍只存服务端。"""
    try:
        x_num = int(str(x).strip())
    except (TypeError, ValueError):
        return False
    with _store_lock:
        rec = _captcha_store.get(captcha_id)
        if not rec:
            return False
        if time.time() - rec["created_at"] > CAPTCHA_TTL:
            _captcha_store.pop(captcha_id, None)
            return False
        target = rec.get("target")
        if target is None:
            return False
        if abs(x_num - target) <= TOLERANCE:
            rec["verified"] = True
            return True
        rec["try_count"] += 1
        if rec["try_count"] > CAPTCHA_MAX_TRIES:
            _captcha_store.pop(captcha_id, None)
        return False


def verify_captcha(captcha_id: str, x: str) -> bool:
    """消费校验（/api/sms/send 边界）：必须已通过滑块（verified）且坐标容差内 → 一次性删除。
    任何失败同样删除，杜绝探测/重放。"""
    try:
        x_num = int(str(x).strip())
    except (TypeError, ValueError):
        with _store_lock:
            _captcha_store.pop(captcha_id, None)
        return False
    with _store_lock:
        rec = _captcha_store.pop(captcha_id, None)
        if not rec:
            return False
        if time.time() - rec["created_at"] > CAPTCHA_TTL:
            return False
        if not rec.get("verified"):
            return False
        target = rec.get("target")
        if target is None:
            return False
        return abs(x_num - target) <= TOLERANCE


def generate_captcha() -> dict:
    """生成一组滑块拼图验证码。

    返回 {captcha_id, width, height, piece_size, target 仅服务端, bg(带凹陷缺口背景), piece(拼图块)}；
    目标 x 坐标仅存服务端（new_captcha_id 时记录 target），绝不下发前端。
    """
    bg = _rand_background()
    target_x = _rand.randrange(20, CAPTCHA_W - PIECE_SIZE - 20)
    piece, bg_hole = _make_piece_and_hole(bg, target_x)

    captcha_id = new_captcha_id(str(target_x))

    return {
        "captcha_id": captcha_id,
        "width": CAPTCHA_W,
        "height": CAPTCHA_H,
        "piece_size": PIECE_SIZE,
        "bg": _img_to_data_uri(bg_hole),
        "piece": _img_to_data_uri(piece),
    }