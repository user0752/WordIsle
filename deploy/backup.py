#!/usr/bin/env python3
"""WordIsle 数据备份脚本（SQLite 在线备份，不锁库）。

用法（服务器 cron，用项目 venv 运行）：
    /opt/wordisle/mvp/venv/bin/python /opt/wordisle-ops/backup.py --keep 7

做的事：
- 备份 mvp/data/system.db 与 mvp/data/user/*.db 到 /opt/wordisle/backups/<时间戳>/
- 用 sqlite3 Connection.backup（WAL 安全），不直接 cp
- 备份目录权限 700，文件权限 600
- 保留最近 --keep 天，超期删除
- --verify      备份后对每个库做 PRAGMA integrity_check
- --with-media  连同 audios/images/videos 一起复制（换服务器迁移时用）

环境变量可覆盖：WORDISLE_DATA_DIR / WORDISLE_BACKUP_DIR
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.getenv("WORDISLE_DATA_DIR", "/opt/wordisle/mvp/data"))
BACKUP_DIR = Path(os.getenv("WORDISLE_BACKUP_DIR", "/opt/wordisle/backups"))


def backup_db(src: Path, dst: Path) -> None:
    """SQLite 在线备份（含 WAL 中未落盘内容）。"""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    os.chmod(dst, 0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description="WordIsle 数据备份")
    ap.add_argument("--keep", type=int, default=7, help="保留天数（默认 7）")
    ap.add_argument("--verify", action="store_true", help="备份后做 SQLite 完整性检查")
    ap.add_argument("--with-media", action="store_true", help="连同 audios/images/videos 一起备份")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not DATA_DIR.exists():
        print(f"[backup] 数据目录不存在：{DATA_DIR}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = BACKUP_DIR / stamp
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    os.chmod(target, 0o700)

    # 保留 data/user 的目录结构：system.db 在根，用户库在 user/ 下，媒体在 media/ 下
    jobs: list[tuple[Path, Path]] = [(DATA_DIR / "system.db", target / "system.db")]
    user_dir = DATA_DIR / "user"
    if user_dir.exists():
        (target / "user").mkdir(parents=True, exist_ok=True)
        os.chmod(target / "user", 0o700)
        jobs += [(p, target / "user" / p.name) for p in sorted(user_dir.glob("*.db")) if p.is_file()]

    ok = failed = 0
    verified = 0
    for src, dst in jobs:
        if not src.exists() or src.stat().st_size == 0:
            continue
        try:
            backup_db(src, dst)
            ok += 1
            if args.verify:
                conn = sqlite3.connect(str(dst))
                try:
                    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    conn.close()
                if result == "ok":
                    verified += 1
                else:
                    failed += 1
                    print(f"[backup] 完整性检查失败 {src.name}: {result}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[backup] 失败 {src.name}: {e}", file=sys.stderr)

    # 媒体文件（换服务器迁移时加 --with-media；日常 cron 不需要）
    media_size = 0
    if args.with_media:
        media_dst = target / "media"
        for sub in ("audios", "images", "videos"):
            src_dir = DATA_DIR / sub
            if src_dir.exists():
                shutil.copytree(src_dir, media_dst / sub, dirs_exist_ok=True)
        if media_dst.exists():
            media_size = sum(p.stat().st_size for p in media_dst.rglob("*") if p.is_file())

    # 清理超期备份
    cutoff = time.time() - args.keep * 86400
    removed = 0
    for d in BACKUP_DIR.iterdir():
        if d.is_dir() and d.name != stamp and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    if not args.quiet:
        size_mb = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) / 1024 / 1024
        extra = f"，完整性检查通过 {verified} 个" if args.verify else ""
        extra += f"，媒体 {media_size / 1024 / 1024:.1f} MB" if args.with_media else ""
        print(f"[backup] {stamp} 成功 {ok} 个，失败 {failed} 个，{size_mb:.1f} MB{extra}，清理 {removed} 个旧备份")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
