"""
词屿 MCP Server —— 把 WordIsle 的单词能力通过 Model Context Protocol（MCP）标准化暴露。

方向 A（对外）：Claude Desktop / Cursor 等任意 MCP 宿主即插即用，
               在外部 AI 里直接查询/维护本系统词库，无需再登录网页端。

安全铁律（沿用词小屿）：
- 查询工具（search_words / get_review_due）：由服务端直接执行，结果回给调用方。
- 写工具（add_words / delete_word）：默认不注册。MCP 调用方也是外部 AI，
  除非在 .env 显式设置 WORDISLE_MCP_ENABLE_WRITE=1 授权，否则不暴露写操作。

服务身份：MCP 场景没有 HTTP 登录，服务端固定以某用户身份读库：
  WORDISLE_MCP_USER 指定用户 uid（默认 DEV_USERNAME，即开发者词库 dev-wordisle.db）。

传输：
  python mcp_server.py                    # 默认 stdio（本地，由宿主进程拉起）
  python mcp_server.py --transport sse    # SSE（远程），默认端口 8001

依赖：pip install "mcp==1.2.0"（已与 fastapi/starlette 版本对齐，勿随意升级 mcp 主版本）
"""

import argparse
import os
import re
import sys

# Windows 下 Python 默认用 GBK 编码 stdout/stderr，而 MCP stdio 协议要求 UTF-8。
# 不强制 UTF-8 时，中文工具描述/日志会以 GBK 字节写出，导致客户端解码失败。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 保证从任意目录启动都能定位项目模块（Claude Desktop / Cursor 用绝对路径拉起时尤其需要）
_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from config import DEV_USERNAME
from db import current_uid, ensure_db_initialized, get_db, setup_stream_logger

# 复用词小屿的查询处理器与文案渲染，同一套业务逻辑、零重复实现
from assistant import _clean_words, _get_review_due, _render_review_due, _render_search, _search_words
from mcp.server.fastmcp import FastMCP
from services import call_word_enrichment

logger = setup_stream_logger("wordisle.mcp")

mcp = FastMCP("wordisle")


def _resolve_uid() -> str:
    """MCP 无 HTTP 登录：以固定用户身份读库（默认开发者词库）。"""
    return os.getenv("WORDISLE_MCP_USER") or DEV_USERNAME


# ========================================================================
# 查询工具（直接执行，复用词小屿逻辑）
# ========================================================================

@mcp.tool()
async def search_words(keyword: str, limit: int = 10) -> str:
    """在用户的单词库中查找单词（精确或模糊匹配），返回词性/中文释义/音标/频率。"""
    r = await _search_words({"keyword": keyword, "limit": limit})
    return _render_search(r)


@mcp.tool()
async def get_review_due(limit: int = 20) -> str:
    """查询记忆测试中今日到期（含积压）的复习单词列表，附每日上限与今日已答数。"""
    r = await _get_review_due({"limit": limit})
    return _render_review_due(r)


# ========================================================================
# 写操作（默认不注册，需 WORDISLE_MCP_ENABLE_WRITE=1 显式授权才暴露）
# ========================================================================

def _register_write_tools() -> None:
    @mcp.tool()
    async def add_words(words: list[str]) -> str:
        """批量添加英文单词到用户词库（重复自动跳过；缺词性/释义/频率时用 AI 自动补全）。"""
        cleaned = _clean_words(words)
        if not cleaned:
            return "没有合法的英文单词可添加。"
        added, skipped = [], []
        for w in cleaned:
            conn = get_db()
            try:
                if conn.execute("SELECT 1 FROM words WHERE word=?", (w,)).fetchone():
                    skipped.append(w)
                    continue
                pos = meaning = freq = ""
                try:
                    enrich = await call_word_enrichment([w])
                    for r in (enrich.get("results") or []):
                        if r.get("word") == w:
                            pos = pos or r.get("pos") or ""
                            meaning = meaning or r.get("meaning_zh") or ""
                            freq = freq or r.get("frequency_level") or ""
                            break
                except Exception as e:
                    logger.warning("mcp add_words 补全失败 word=%s error=%r", w, e)
                conn.execute(
                    "INSERT INTO words (word, pos, meaning_zh, frequency_level, frequency_source) "
                    "VALUES (?,?,?,?,'mcp')",
                    (w, pos, meaning, freq),
                )
                conn.commit()
                added.append(w)
            except Exception as e:
                logger.warning("mcp add_words 入库失败 word=%s error=%r", w, e)
            finally:
                conn.close()
        parts = [f"已添加 {len(added)} 个：{'、'.join(added)}"] if added else []
        if skipped:
            parts.append(f"已存在跳过 {len(skipped)} 个：{'、'.join(skipped)}")
        return "\n".join(parts) if parts else "没有新单词被添加。"

    @mcp.tool()
    async def delete_word(word: str) -> str:
        """从用户词库中删除一个单词（同步清理其复习排期）。"""
        w = re.sub(r"[^a-zA-Z\-']", "", str(word or "").strip().lower())
        if not w:
            return "请提供要删除的单词。"
        conn = get_db()
        try:
            row = conn.execute("SELECT id FROM words WHERE word=?", (w,)).fetchone()
            if not row:
                return f"词库中没有「{w}」，无需删除。"
            conn.execute("DELETE FROM review_schedule WHERE word=?", (w,))
            conn.execute("DELETE FROM words WHERE word=?", (w,))
            conn.commit()
            return f"已从词库删除「{w}」。"
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="WordIsle MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="传输方式")
    parser.add_argument("--port", type=int, default=8001, help="SSE 传输的监听端口")
    args = parser.parse_args()

    uid = _resolve_uid()
    current_uid.set(uid)
    ensure_db_initialized(uid)  # 确保目标用户分库存在

    write_on = os.getenv("WORDISLE_MCP_ENABLE_WRITE") == "1"
    if write_on:
        _register_write_tools()

    logger.info("wordisle MCP Server 启动 user=%s transport=%s 写操作=%s",
                uid, args.transport, "开启" if write_on else "关闭")

    if args.transport == "sse":
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
