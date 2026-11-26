"""成本熔断：全站每日文生图张数上限。

- 单进程内存态，按自然日重置；重启会清零（可接受，属于兜底而非精确计费）。
- 所有图片生成都收口到 services.call_image_generation，在真正发请求前扣减预算，
  避免被刷时产生真实账单。
"""

import threading
import time
from datetime import date

from fastapi import HTTPException

from config import DAILY_IMAGE_LIMIT

_lock = threading.Lock()
_day = ""
_count = 0


def consume_image_budget(n: int = 1) -> None:
    """占用 n 张图片预算；超出当日上限抛 429。"""
    global _day, _count
    today = date.today().isoformat()
    with _lock:
        if _day != today:
            _day, _count = today, 0
        if _count + n > DAILY_IMAGE_LIMIT:
            raise HTTPException(429, "今日全站图片生成额度已用完，请明天再试")
        _count += n


def image_budget_status() -> dict:
    today = date.today().isoformat()
    with _lock:
        used = _count if _day == today else 0
    return {"day": today, "used": used, "limit": DAILY_IMAGE_LIMIT}
