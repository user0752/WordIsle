#!/usr/bin/env python3
"""清理长期不活跃的游客库（默认 dry-run，不删任何东西）。

背景：游客免登录创建，每个游客一个 SQLite 文件，长期不清理会无限增长。
策略：只处理 role='guest'，且库文件最后修改时间早于 --days 天；正式账号永不触碰。

用法：
    python guest_cleanup.py                 # 只统计
    python guest_cleanup.py --days 60 --apply   # 真删（先备份！）
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("WORDISLE_DATA_DIR", "/opt/wordisle/mvp/data"))
SYSTEM_DB = DATA_DIR / "system.db"
USER_DIR = DATA_DIR / "user"


def main() -> int:
    ap = argparse.ArgumentParser(description="清理不活跃游客库")
    ap.add_argument("--days", type=int, default=90, help="超过多少天未活动（按库文件 mtime，默认 90）")
    ap.add_argument("--apply", action="store_true", help="执行删除；缺省仅统计")
    args = ap.parse_args()

    if not SYSTEM_DB.exists():
        print(f"[guest-cleanup] 找不到 {SYSTEM_DB}", file=sys.stderr)
        return 1
    cutoff = time.time() - args.days * 86400

    conn = sqlite3.connect(str(SYSTEM_DB))
    conn.row_factory = sqlite3.Row
    guests = conn.execute("SELECT uid FROM users WHERE role='guest'").fetchall()

    candidates = []
    total_bytes = 0
    for row in guests:
        uid = row["uid"]
        db = USER_DIR / f"{uid}.db"
        if not db.exists():
            continue
        if db.stat().st_mtime < cutoff:
            size = db.stat().st_size
            candidates.append((uid, size))
            total_bytes += size

    print(f"[guest-cleanup] 游客总数 {len(guests)}，超过 {args.days} 天不活跃 {len(candidates)} 个，"
          f"合计 {total_bytes / 1024 / 1024:.1f} MB")
    for uid, size in candidates[:20]:
        print(f"  - {uid}  {size / 1024:.0f} KB")
    if len(candidates) > 20:
        print(f"  ... 其余 {len(candidates) - 20} 个省略")

    if not args.apply:
        print("[guest-cleanup] dry-run，未删除。确认后加 --apply 执行（建议先跑 backup.py）。")
        return 0

    deleted = 0
    for uid, _size in candidates:
        for suffix in ("", "-wal", "-shm"):
            p = USER_DIR / f"{uid}.db{suffix}"
            if p.exists():
                p.unlink(missing_ok=True)
        conn.execute("DELETE FROM users WHERE uid=? AND role='guest'", (uid,))
        conn.execute("DELETE FROM quotas WHERE uid=?", (uid,))
        deleted += 1
    conn.commit()
    conn.close()
    print(f"[guest-cleanup] 已删除 {deleted} 个游客库及其账号/配额记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
