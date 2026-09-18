"""滑块拼图图形验证码（Pillow 位图渲染）。

- 主流滑块拼图交互（极验/腾讯云同款）：随机位图背景 + 拼图块，
  前端拖滑块把拼图块对准缺口，服务端比对 x 坐标（容差内通过）。
- 位图而非 SVG：矢量缺口坐标可被脚本直接解析，位图只有 CV 才能定位，
  达到主流验证码同等防护。
- 答案=目标 x 坐标（服务端内存态存储，不下发前端）；内存态一次性，TTL 过期、
  尝试次数作废、待校验数量上限防内存刷爆。
"""
import base64
import io
import random
import secrets
import threading
import time

from PIL import Image, ImageDraw

from config import CAPTCHA_MAX_TRIES, CAPTCHA_TTL

# secrets 无 randrange（那是 random 的），用 SystemRandom 做加密安全随机
_rand = random.SystemRandom()

# 验证码存储：{captcha_id: {"target": int|None, "created_at": float, "try_count": int}}
_captcha_store: dict[str, dict] = {}
_store_lock = threading.Lock()

# 防止内存被刷爆：未过期待校验验证码上限，超限时淘汰最旧一条
CAPTCHA_MAX_PENDING = 10000

# 画布与拼图参数（px）
CAPTCHA_W = 300
CAPTCHA_H = 100
PIECE_SIZE = 56          # 拼图块边长（方形）
PIECE_Y = (CAPTCHA_H - PIECE_SIZE) // 2   # 垂直居中放置
TOLERANCE = 12           # x 偏差容差（px）

# 背景随机色板（读起来舒适的浅色系，深浅各一套便于随机组合）
_PALETTES = [
    ((120, 190, 180), (235, 245, 240)),   # 绿
    ((150, 170, 220), (240, 244, 252)),   # 蓝
    ((215, 165, 120), (252, 244, 234)),   # 橙
    ((170, 150, 205), (246, 242, 252)),   # 紫
    ((200, 150, 150), (250, 242, 242)),   # 红
]


def _rand_color(base, spread=28):
    """在基准色附近随机抖动，避免每次都同色。"""
    return tuple(
        max(0, min(255, c + _rand.randrange(-spread, spread + 1))) for c in base
    )


def _rand_background() -> Image.Image:
    """生成随机位图背景：渐变底色 + 若干几何色块 + 干扰线/噪点。"""
    deep, light = secrets.choice(_PALETTES)
    img = Image.new("RGB", (CAPTCHA_W, CAPTCHA_H), _rand_color(light))
    draw = ImageDraw.Draw(img)
    # 对角渐变：整幅叠加一条半透明深色带，增加纹理
    for i in range(CAPTCHA_W // 2):
        alpha = int(14 * (1 - i / (CAPTCHA_W / 2)))
        draw.line(
            [(i, 0), (i + CAPTCHA_H, CAPTCHA_H)], fill=(*_rand_color(deep, 10), alpha), width=1,
        )
    # 随机几何色块（圆/椭圆/矩形），颜色取自深色系
    for _ in range(_rand.randrange(6, 11)):
        x = _rand.randrange(0, CAPTCHA_W)
        y = _rand.randrange(0, CAPTCHA_H)
        w = _rand.randrange(18, 70)
        h = _rand.randrange(12, 40)
        color = _rand_color(deep, 14)
        kind = _rand.randrange(3)
        if kind == 0:
            draw.ellipse([x, y, x + w, y + h], fill=color)
        else:
            draw.rectangle([x, y, x + w, y + h], fill=color)
    # 干扰线
    for _ in range(6):
        draw.line(
            [(_rand.randrange(0, CAPTCHA_W), _rand.randrange(0, CAPTCHA_H)),
             (_rand.randrange(0, CAPTCHA_W), _rand.randrange(0, CAPTCHA_H))],
            fill=(*_rand_color(deep, 20), 0), width=1,
        )
    # 噪点
    for _ in range(60):
        draw.point(
            (_rand.randrange(0, CAPTCHA_W), _rand.randrange(0, CAPTCHA_H)),
            fill=_rand_color(deep, 40),
        )
    return img


def _draw_hole(bg: Image.Image, target_x: int) -> Image.Image:
    """在目标位置挖方形洞：填充偏白/同底色并加深色描边，形成可见缺口。"""
    img = bg.copy()
    draw = ImageDraw.Draw(img)
    # 先取该区域平均色做洞底（接近背景，缺口自然）
    crop = bg.crop((target_x, PIECE_Y, target_x + PIECE_SIZE, PIECE_Y + PIECE_SIZE))
    pixels = list(crop.convert("RGB").getdata())
    if pixels:
        avg = tuple(sum(ch) // len(pixels) for ch in zip(*pixels))
    else:
        avg = (245, 245, 245)
    hole = tuple(min(255, c + 26) for c in avg)  # 略提亮，显洞
    draw.rectangle(
        [target_x, PIECE_Y, target_x + PIECE_SIZE, PIECE_Y + PIECE_SIZE], fill=hole,
    )
    # 深色描边（缺口轮廓）
    draw.rectangle(
        [target_x, PIECE_Y, target_x + PIECE_SIZE, PIECE_Y + PIECE_SIZE],
        outline=_rand_color((40, 60, 55), 12), width=2,
    )
    return img


def _img_to_data_uri(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/{fmt.lower()};base64,{b64}"


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
        }
    return captcha_id


def _purge_expired_locked():
    """清理过期项（须持有 _store_lock 时调用）。"""
    now = time.time()
    dead = [cid for cid, v in _captcha_store.items() if now - v["created_at"] > CAPTCHA_TTL]
    for cid in dead:
        _captcha_store.pop(cid, None)


def verify_captcha(captcha_id: str, x: str) -> bool:
    """校验滑块验证码：|x - target| <= TOLERANCE 且一次性/未过期/次数未超限。
    无论对错均删除（一次性）；尝试超 5 次作废。返回是否通过。"""
    try:
        x_num = int(str(x).strip())
    except (TypeError, ValueError):
        return False
    with _store_lock:
        rec = _captcha_store.pop(captcha_id, None)
        if not rec:
            return False
        rec["try_count"] += 1
        if rec["try_count"] > CAPTCHA_MAX_TRIES:
            return False
        if time.time() - rec["created_at"] > CAPTCHA_TTL:
            return False
        target = rec.get("target")
    if target is None:
        return False
    return abs(x_num - target) <= TOLERANCE


def generate_captcha() -> dict:
    """生成一组滑块拼图验证码。

    返回 {captcha_id, width, height, piece_size, bg(带洞背景), piece(拼图块)}；
    目标 x 坐标仅存服务端（new_captcha_id 时记录 target），不下发前端。
    """
    bg = _rand_background()
    target_x = _rand.randrange(20, CAPTCHA_W - PIECE_SIZE - 20)
    # 拼图块：背景上与缺口对应位置的原图案
    piece = bg.crop((target_x, PIECE_Y, target_x + PIECE_SIZE, PIECE_Y + PIECE_SIZE))
    bg_hole = _draw_hole(bg, target_x)

    captcha_id = new_captcha_id(str(target_x))

    return {
        "captcha_id": captcha_id,
        "width": CAPTCHA_W,
        "height": CAPTCHA_H,
        "piece_size": PIECE_SIZE,
        "bg": _img_to_data_uri(bg_hole),
        "piece": _img_to_data_uri(piece),
    }