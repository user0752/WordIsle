#!/usr/bin/env bash
# 从备份目录恢复 WordIsle 数据（换服务器 / 灾难恢复用）
# 用法：sudo bash deploy/restore.sh /opt/wordisle/backups/2026-11-26_030000
#
# 行为：停服务 → 现有 data 改名为 data.bak-<时间> → 恢复 system.db/user/*.db/media
#       → chown www-data → 启动 → 等 /healthz
# 注意：会覆盖当前数据；确认备份目录正确再执行。
set -euo pipefail

BACKUP="${1:-}"
DATA="${DATA_DIR:-/opt/wordisle/mvp/data}"

if [[ -z "$BACKUP" || ! -d "$BACKUP" ]]; then
  echo "用法: sudo bash deploy/restore.sh <备份目录>"
  exit 1
fi
[[ $EUID -eq 0 ]] || { echo "请用 root 运行"; exit 1; }

systemctl stop wordisle 2>/dev/null || true
STAMP="$(date +%Y%m%d_%H%M%S)"
if [[ -d "$DATA" ]]; then
  mv "$DATA" "${DATA}.bak-$STAMP"
fi
mkdir -p "$DATA/user"

# 全局库
if [[ -f "$BACKUP/system.db" ]]; then
  cp -a "$BACKUP/system.db" "$DATA/system.db"
fi

# 用户库：新格式在 user/ 下；旧格式平铺在备份根目录
if [[ -d "$BACKUP/user" ]]; then
  cp -a "$BACKUP/user/." "$DATA/user/"
else
  find "$BACKUP" -maxdepth 1 -type f -name "*.db" ! -name "system.db" \
    -exec cp -a {} "$DATA/user/" \;
fi

# 媒体文件（备份时加 --with-media 才有）
if [[ -d "$BACKUP/media" ]]; then
  for sub in audios images videos; do
    if [[ -d "$BACKUP/media/$sub" ]]; then
      mkdir -p "$DATA/$sub"
      cp -a "$BACKUP/media/$sub/." "$DATA/$sub/"
    fi
  done
fi

chown -R www-data:www-data "$DATA"
systemctl start wordisle

for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "恢复完成；旧数据保留在 ${DATA}.bak-$STAMP"
    exit 0
  fi
  sleep 1
done

echo "服务未在 20 秒内就绪，请查看：journalctl -u wordisle -n 100 --no-pager"
exit 1
