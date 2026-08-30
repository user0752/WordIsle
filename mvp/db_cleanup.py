"""
数据体检与清理脚本（Phase B · 一次性治理）
=========================================
对指定业务库（默认开发者库）执行数据体检与治理：

  1) 删除已识别的测试残留词（黑名单：inplicate / indegestion / deprecated / meeting）
  2) 清理释义中的会话残留元信息（如"（技术语境中常指…）"这类 AI 自我注释）
  3) 顺带清掉 polysemy 表中同名单的黑名单词（防泄漏到熟词僻意候选）

用法（在 mvp 目录下，用项目 venv 的 python）：
  python db_cleanup.py                 # 只体检，输出报告（dry-run，不修改）
  python db_cleanup.py --db <path>     # 体检指定库（如服务器库 dev-wordisle.db）
  python db_cleanup.py --apply         # 体检并执行清理（删除/修正）

安全：不带 --apply 时绝不修改数据；--apply 前建议整库备份。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from db import AI_WORD_BLACKLIST, clean_meaning_residue


def inspect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        words_del, words_fix = [], []
        for r in conn.execute(
            "SELECT id, word, meaning_zh FROM words ORDER BY id"
        ).fetchall():
            w = (r["word"] or "").strip().lower()
            if w in AI_WORD_BLACKLIST:
                words_del.append({"id": r["id"], "word": r["word"], "why": "黑名单测试残留词"})
                continue
            clean = clean_meaning_residue(r["meaning_zh"] or "")
            if clean != (r["meaning_zh"] or ""):
                words_fix.append({"id": r["id"], "word": r["word"], "before": (r["meaning_zh"] or "")[:60], "after": clean[:60]})

        poly_del = []
        for r in conn.execute("SELECT id, word FROM polysemy ORDER BY id").fetchall():
            if (r["word"] or "").strip().lower() in AI_WORD_BLACKLIST:
                poly_del.append({"id": r["id"], "word": r["word"]})

        return words_del, words_fix, poly_del
    finally:
        conn.close()


def apply(db_path: str, words_del, words_fix, poly_del):
    conn = sqlite3.connect(db_path)
    try:
        for it in words_del:
            conn.execute("DELETE FROM words WHERE id=?", (it["id"],))
        for it in words_fix:
            conn.execute("UPDATE words SET meaning_zh=? WHERE id=?", (it["after"], it["id"]))
        for it in poly_del:
            conn.execute("DELETE FROM polysemy WHERE id=?", (it["id"],))
        conn.commit()
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="WordIsle 数据体检清理（Phase B）")
    ap.add_argument("--db", default="data/user/dev-wordisle.db", help="业务库路径（默认开发者库）")
    ap.add_argument("--apply", action="store_true", help="执行清理；缺省仅输出体检报告")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"[错误] 数据库不存在：{args.db}")
        sys.exit(1)

    words_del, words_fix, poly_del = inspect(args.db)
    print(f"==== 数据体检报告：{args.db} ====")
    print(f"· 待删除测试残留词（words）：{len(words_del)} 个")
    for it in words_del:
        print(f"    ✗ [{it['id']}] {it['word']} — {it['why']}")
    print(f"· 待清理释义残留（words）：{len(words_fix)} 个")
    for it in words_fix:
        print(f"    ✎ [{it['id']}] {it['word']}：{it['before']} → {it['after']}")
    print(f"· 待删除黑名单词（polysemy）：{len(poly_del)} 个")
    for it in poly_del:
        print(f"    ✗ [{it['id']}] {it['word']}")

    if not args.apply:
        print("\n[dry-run] 未做任何修改。确认无误后加 --apply 执行清理。")
        return

    apply(args.db, words_del, words_fix, poly_del)
    print(f"\n[已执行] 删除 words {len(words_del)} 个、修正释义 {len(words_fix)} 个、删除 polysemy {len(poly_del)} 个。")


if __name__ == "__main__":
    main()
