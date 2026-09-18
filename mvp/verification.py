"""图形验证码（零依赖 SVG）+ 短信验证码共用工具。

- SVG 验证码：4 位字符 + 干扰线，内存态存储（单进程 uvicorn 足够），
  一次性、300s 过期、尝试 5 次作废。
- 提供 phone / captcha 校验辅助函数，供 auth.py 端点使用。
"""
import hashlib
import secrets
import threading
import time

from config import CAPTCHA_MAX_TRIES, CAPTCHA_TTL

# 验证码存储：{captcha_id: {"answer_hash": str, "created_at": float, "try_count": int}}
_captcha_store: dict[str, dict] = {}
_store_lock = threading.Lock()

# 防止内存被刷爆：未过期待校验验证码上限，超限时淘汰最旧一条
CAPTCHA_MAX_PENDING = 10000

# 可读字符集（去掉易混淆的 0/O、1/l/I）
CAPTCHA_CHARS = "23456789abcdefghjkmnpqrstuvwxyz"

# 验证码画布
_WIDTH, _HEIGHT = 150, 50


def _hex_color(seed: int) -> str:
    """由整数种子生成一个可读的深色前景色。"""
    r = 30 + (seed * 37) % 160
    g = 30 + (seed * 71) % 160
    b = 30 + (seed * 113) % 160
    return f"#{r:02x}{g:02x}{b:02x}"


def new_captcha_id(answer: str) -> str:
    """生成新验证码：预存答案哈希，返回 captcha_id。"""
    captcha_id = secrets.token_hex(16)
    with _store_lock:
        _purge_expired_locked()
        # 超限保护：未过期待校验验证码超过上限时，依次淘汰最旧的
        while len(_captcha_store) >= CAPTCHA_MAX_PENDING:
            oldest = next(iter(_captcha_store))
            _captcha_store.pop(oldest, None)
        _captcha_store[captcha_id] = {
            "answer_hash": hashlib.sha256(answer.encode()).hexdigest(),
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


def verify_captcha(captcha_id: str, answer: str) -> bool:
    """校验图形验证码：一次性（无论对错均删除），尝试 5 次作废。
    返回是否通过。answer 需先转小写（生成时即小写）。"""
    with _store_lock:
        rec = _captcha_store.pop(captcha_id, None)
        if not rec:
            return False
        rec["try_count"] += 1
        if rec["try_count"] > CAPTCHA_MAX_TRIES:
            return False
        if time.time() - rec["created_at"] > CAPTCHA_TTL:
            return False
        expected = rec["answer_hash"]
    return hashlib.sha256(answer.strip().lower().encode()).hexdigest() == expected


# 5x7 点阵字体（验证码字符集）。每字符 5 列 x 7 行，'#' 表示该格点亮。
# 用点阵渲染而非 <text>，避免机器直接解析明文文本绕过图形验证码。
_CAPTCHA_DOT_FONT = {
    "2": ("01110", "10001", "00001", "00110", "01000", "10000", "11111"),
    "3": ("11111", "00001", "00010", "00110", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "a": ("01110", "00001", "01111", "10001", "10011", "01101", "00000"),
    "b": ("10000", "10000", "10110", "11001", "10001", "10001", "01110"),
    "c": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "d": ("00001", "00001", "01101", "10011", "10001", "10001", "01111"),
    "e": ("01110", "10001", "10001", "11111", "10000", "10000", "01110"),
    "f": ("00110", "01001", "01000", "11100", "01000", "01000", "01000"),
    "g": ("01110", "10001", "10001", "01111", "00001", "10001", "01110"),
    "h": ("10000", "10000", "10110", "11001", "10001", "10001", "10001"),
    "j": ("00010", "00010", "00010", "00010", "00010", "10010", "01100"),
    "k": ("10000", "10000", "10010", "10100", "11000", "10100", "10010"),
    "m": ("00000", "00000", "11011", "10101", "10101", "10101", "10101"),
    "n": ("00000", "00000", "10110", "11001", "10001", "10001", "10001"),
    "p": ("00000", "00000", "01110", "10001", "10001", "10001", "01110"),
    "q": ("00000", "00000", "01101", "10011", "10001", "10001", "01111"),
    "r": ("00000", "00000", "10110", "11001", "10000", "10000", "10000"),
    "s": ("00000", "00000", "01111", "10000", "01110", "00001", "11110"),
    "t": ("01000", "01000", "11100", "01000", "01000", "01001", "00110"),
    "u": ("00000", "00000", "10001", "10001", "10001", "10011", "01101"),
    "v": ("00000", "00000", "10001", "10001", "10001", "01010", "00100"),
    "w": ("00000", "00000", "10101", "10101", "10101", "10101", "01010"),
    "x": ("00000", "00000", "10001", "01010", "00100", "01010", "10001"),
    "y": ("00000", "10001", "10001", "01010", "00100", "01000", "10000"),
    "z": ("00000", "00000", "11111", "00010", "00100", "01000", "11111"),
}

# 5x7 点阵的格子像素尺寸（放大后）
_DOT_SCALE = 4
_DOT_W = 5 * _DOT_SCALE      # 35
_DOT_H = 7 * _DOT_SCALE      # 28


def _render_char_dots(ch: str, offset_x: float, offset_y: float, color: str, seed: int, rects: list[str]):
    """把一个字符渲染为 point 方块矩形（无文本），可整体旋转。"""
    rows = _CAPTCHA_DOT_FONT.get(ch, _CAPTCHA_DOT_FONT["a"])
    for r_i, row in enumerate(rows):
        for c_i, cell in enumerate(row):
            if cell != "1":   # 字体表用 '1' 表示点亮格
                continue
            # 每个点亮格做轻微抖动，打破整齐网格、干扰机器识别
            jx = hash(f"jx-{ch}-{r_i}-{c_i}-{seed}") % 3 - 1
            jy = hash(f"jy-{ch}-{r_i}-{c_i}-{seed}") % 3 - 1
            x = offset_x + c_i * _DOT_SCALE + jx
            y = offset_y + r_i * _DOT_SCALE + jy
            rects.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{_DOT_SCALE - 0.6:.2f}" '
                f'height="{_DOT_SCALE - 0.6:.2f}" fill="{color}"/>'
            )


def generate_captcha_svg(text: str) -> str:
    """把 4 位字符渲染为 5x7 点阵 SVG（无明文文本，干扰线+旋转抗机器解析）。"""
    if len(text) != 4:
        text = (text + "abcd")[:4]
    seed = sum(ord(c) for c in text)
    color = _hex_color(seed)
    width = _WIDTH

    char_w = _DOT_W + 6          # 每个字符左右留白
    start_x = (width - 4 * char_w) / 2 + 3
    # 用 <g> 分组逐个字符轻微旋转
    g_parts = []
    for i, ch in enumerate(text):
        cx = start_x + i * char_w
        cy = (_HEIGHT - _DOT_H) / 2
        rot = (hash(f"r-{ch}-{i}-{seed}") % 26) - 13
        cx_c, cy_c = cx + _DOT_W / 2, cy + _DOT_H / 2
        inner: list[str] = []
        _render_char_dots(ch, cx, cy, color, seed, inner)
        g_parts.append(
            f'<g transform="rotate({rot} {cx_c:.1f} {cy_c:.1f})">{"".join(inner)}</g>'
        )
    # 干扰线
    lines = []
    for k in range(6):
        x1 = hash(f"a-{k}-{seed}") % width
        y1 = hash(f"b-{k}-{seed}") % _HEIGHT
        x2 = hash(f"c-{k}-{seed}") % width
        y2 = hash(f"d-{k}-{seed}") % _HEIGHT
        lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="rgba(0,0,0,0.18)" stroke-width="1.1"/>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{_HEIGHT}" '
        f'viewBox="0 0 {width} {_HEIGHT}"><rect width="100%" height="100%" fill="#f5efe2"/>'
        + "".join(lines + g_parts)
        + "</svg>"
    )


def generate_captcha() -> tuple[str, str]:
    """生成一组验证码。返回 (captcha_id, svg_html)。"""
    answer = "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(4))
    captcha_id = new_captcha_id(answer)
    return captcha_id, generate_captcha_svg(answer)