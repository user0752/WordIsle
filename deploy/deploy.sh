#!/usr/bin/env bash
# WordIsle 全新服务器部署脚本（Ubuntu 22.04 / 24.04，root 运行）
#
# 用法：
#   sudo DOMAIN=wordisle.example.com EMAIL=you@example.com bash deploy/deploy.sh
#   sudo bash deploy/deploy.sh                 # 无域名：仅 HTTP，用 IP 访问
#
# 前置：
#   - 仓库已 clone 到 /opt/wordisle（或脚本会用 REPO 自动 clone）
#   - 已有 deploy 用户并配置 SSH key（用于后续 git pull）；没有则用 root 管理代码
#   - 有域名时，DNS A 记录已指向本机公网 IP
#
# 脚本可重复执行：已存在的目录/账号/cron 会跳过或覆盖为最新模板。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/wordisle}"
OPS_DIR="${OPS_DIR:-/opt/wordisle-ops}"
REPO="${REPO:-https://github.com/user0752/WordIsle.git}"
CODE_USER="${CODE_USER:-deploy}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"

log() { echo -e "\n[deploy] $*"; }
[[ $EUID -eq 0 ]] || { echo "请用 root 运行（sudo bash deploy/deploy.sh）"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

if ! id "$CODE_USER" >/dev/null 2>&1; then
  log "未找到用户 $CODE_USER，代码改由 root 管理（后续 git pull 也用 root）"
  CODE_USER=root
fi

log "1/10 安装系统依赖"
apt-get update -y
apt-get install -y python3-venv python3-pip nginx sqlite3 git ufw fail2ban curl
if [[ -n "$DOMAIN" ]]; then
  apt-get install -y certbot python3-certbot-nginx
fi

log "2/10 时区（每日配额/复习按本地日切）"
timedatectl set-timezone Asia/Shanghai

log "3/10 代码"
if [[ ! -d "$APP_DIR/.git" ]]; then
  mkdir -p "$(dirname "$APP_DIR")"
  git clone "$REPO" "$APP_DIR"
fi
chown -R "$CODE_USER":www-data "$APP_DIR"
chmod -R g+rX "$APP_DIR"

log "4/10 虚拟环境与依赖"
if [[ ! -x "$APP_DIR/mvp/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/mvp/venv"
fi
"$APP_DIR/mvp/venv/bin/pip" install --upgrade pip
"$APP_DIR/mvp/venv/bin/pip" install -r "$APP_DIR/mvp/requirements.txt"

log "5/10 数据/日志/备份目录"
mkdir -p "$APP_DIR/mvp/data/user" "$APP_DIR/mvp/data/audios" \
         "$APP_DIR/mvp/data/images" "$APP_DIR/mvp/data/videos" \
         "$APP_DIR/mvp/logs" "$APP_DIR/logs" "$APP_DIR/backups" "$OPS_DIR"
chown -R www-data:www-data "$APP_DIR/mvp/data" "$APP_DIR/mvp/logs" "$APP_DIR/backups"
chmod 700 "$APP_DIR/backups"
chown "$CODE_USER":www-data "$APP_DIR/logs" "$OPS_DIR"
chmod 775 "$APP_DIR/logs" "$OPS_DIR"

log "6/10 环境变量"
if [[ ! -f "$APP_DIR/mvp/.env" ]]; then
  cp "$APP_DIR/deploy/env.production.example" "$APP_DIR/mvp/.env"
  chown "$CODE_USER":www-data "$APP_DIR/mvp/.env"
  chmod 640 "$APP_DIR/mvp/.env"
  echo "已生成 $APP_DIR/mvp/.env，请填写全部必填项后重新运行本脚本。"
  exit 1
fi
if grep -q "请替换" "$APP_DIR/mvp/.env"; then
  echo "$APP_DIR/mvp/.env 仍含“请替换”占位符，请先填写。"
  exit 1
fi
chown "$CODE_USER":www-data "$APP_DIR/mvp/.env"
chmod 640 "$APP_DIR/mvp/.env"

# 按是否启用 HTTPS 自动设置 Cookie Secure 与允许来源
if [[ -n "$DOMAIN" ]]; then
  sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=1/' "$APP_DIR/mvp/.env"
  sed -i "s#^CORS_ORIGINS=.*#CORS_ORIGINS=https://$DOMAIN#" "$APP_DIR/mvp/.env"
else
  sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=0/' "$APP_DIR/mvp/.env"
  sed -i 's/^CORS_ORIGINS=.*/CORS_ORIGINS=/' "$APP_DIR/mvp/.env"
fi

log "7/10 systemd"
install -m 644 "$APP_DIR/deploy/wordisle.service" /etc/systemd/system/wordisle.service
systemctl daemon-reload
systemctl enable wordisle
systemctl restart wordisle

log "8/10 备份与巡检脚本"
install -m 755 "$APP_DIR/deploy/backup.py" "$OPS_DIR/backup.py"
install -m 755 "$APP_DIR/deploy/guest_cleanup.py" "$OPS_DIR/guest_cleanup.py"
CRON_TMP="$(mktemp)"
crontab -l > "$CRON_TMP" 2>/dev/null || true
grep -q "$OPS_DIR/backup.py" "$CRON_TMP" || cat >> "$CRON_TMP" <<EOF
0 3 * * * WORDISLE_DATA_DIR=$APP_DIR/mvp/data WORDISLE_BACKUP_DIR=$APP_DIR/backups runuser -u www-data -- $APP_DIR/mvp/venv/bin/python $OPS_DIR/backup.py --keep 7 --verify >> $APP_DIR/logs/backup.log 2>&1
EOF
grep -q "$OPS_DIR/guest_cleanup.py" "$CRON_TMP" || cat >> "$CRON_TMP" <<EOF
30 3 * * 0 WORDISLE_DATA_DIR=$APP_DIR/mvp/data runuser -u www-data -- $APP_DIR/mvp/venv/bin/python $OPS_DIR/guest_cleanup.py --days 90 >> $APP_DIR/logs/guest_cleanup.log 2>&1
EOF
grep -q "ops_monitor.py" "$CRON_TMP" || cat >> "$CRON_TMP" <<EOF
*/15 * * * * cd $APP_DIR/mvp && $APP_DIR/mvp/venv/bin/python ops_monitor.py --check >> $APP_DIR/logs/ops_check.log 2>&1
30 7 * * * cd $APP_DIR/mvp && $APP_DIR/mvp/venv/bin/python ops_monitor.py --report >> $APP_DIR/logs/ops_report.log 2>&1
EOF
crontab "$CRON_TMP"
rm -f "$CRON_TMP"

log "9/10 nginx"
if [[ -n "$DOMAIN" ]]; then
  sed "s/server_name _;/server_name $DOMAIN;/" "$APP_DIR/deploy/nginx-wordisle.conf" > /etc/nginx/sites-available/wordisle
else
  install -m 644 "$APP_DIR/deploy/nginx-wordisle.conf" /etc/nginx/sites-available/wordisle
fi
install -m 644 "$APP_DIR/deploy/nginx-limits.conf" /etc/nginx/conf.d/wordisle-limits.conf
ln -sf /etc/nginx/sites-available/wordisle /etc/nginx/sites-enabled/wordisle
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if [[ -n "$DOMAIN" && -n "$EMAIL" ]]; then
  log "10/10 申请 Let's Encrypt 证书并启用 HTTPS"
  certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive --redirect
  nginx -t
  systemctl reload nginx
else
  log "10/10 跳过 HTTPS（未提供 DOMAIN/EMAIL）；有域名后重跑或手动执行 certbot"
fi

log "防火墙"
ufw allow OpenSSH
ufw allow 80/tcp
if [[ -n "$DOMAIN" ]]; then ufw allow 443/tcp; fi
ufw --force enable
systemctl enable --now fail2ban

log "健康检查"
READY=0
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then READY=1; break; fi
  sleep 1
done
if [[ "$READY" != "1" ]]; then
  echo "服务未在 20 秒内就绪，请查看：journalctl -u wordisle -n 100 --no-pager"
  exit 1
fi
curl -fsS http://127.0.0.1:8000/healthz && echo
log "完成：http://$(hostname -I | awk '{print $1}')/  （域名：${DOMAIN:-未配置}）"
