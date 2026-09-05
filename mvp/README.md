# WordIsle · 部署与运维参考

> 项目主文档见仓库根目录 [README.md](../README.md)。本文件仅记录生产部署与运维细节。

**部署形态**：FastAPI + SQLite + Vue3 单应用，零 Redis/Nginx/Celery/Postgres 等基础设施；2C/2G 的 Ubuntu/Debian 服务器即可常驻运行。

## 启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key（DeepSeek / 阿里云百炼，见 .env.example 内注释）
cp .env.example .env

# 3. 启动
python main.py

# 4. 浏览器打开
# http://localhost:8000
```

也可使用项目自带的 `manager.py`（GUI / `--cli` 终端模式）或 Windows `start.bat` / Linux `start.sh`。

## Linux 服务器部署

> 面向无桌面的 Ubuntu/Debian 服务器。应用背后是付费 AI 接口，**公网放行前必须先做访问控制**（见下文），否则任何访客都能触发文生图/文生视频等付费调用。

### 1. 前置依赖

```bash
sudo apt update
sudo apt install -y ffmpeg python3 python3-venv
sudo apt install -y fonts-noto-cjk   # 建议安装（当前英文字幕用 DejaVu 即可，装上可兼容未来中文字幕）
```

`services.py` 会自动优先选用支持 drawtext 的系统 ffmpeg，仅当系统缺失时回退到 `imageio-ffmpeg` 自带二进制。

### 2. 快速启动（start.sh）

```bash
cd /path/to/mvp
./start.sh          # 自动：初始化 .env → 建 venv 装依赖 → mkdir 数据目录 → 启动
```

等价于 Windows 的 `start.bat`。需可执行权限：`chmod +x start.sh`。

### 3. systemd 常驻（推荐）

```bash
sudo cp deploy/wordisle.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wordisle
sudo systemctl status wordisle
journalctl -u wordisle -f        # 查看日志
```

服务以 `www-data` 用户运行，首次部署需授权数据目录：

```bash
sudo chown -R www-data:www-data /path/to/mvp/data /path/to/mvp/logs
```

### 4. 公网访问控制（必需，三选一）

* **A 限源 IP**（仅自己用）：云安全组 + `ufw` 只放行自己的公网 IP 到 8000

* **B Nginx + Basic Auth**（分享给他人）：`auth_basic` + `proxy_pass http://127.0.0.1:8000`，对外只开 80

* **C SSH 隧道**（最安全）：`ssh -N -L 8000:127.0.0.1:8000 deploy@<服务器IP>`，本地访问 `http://localhost:8000`

### 5. 无 tkinter 环境

服务器未装 GUI 时，启动管理器需以 CLI 模式运行且无 tkinter 依赖：

```bash
python manager.py --cli      # 服务端环境可直接使用
```

Windows GUI 模式不受影响。

### 6. 验证清单

* [ ] `venv/bin/python main.py` 后 `http://<IP>:8000` 可访问，首页样式正常

* [ ] 触发一次视频编译，字幕烧录无字体/ffmpeg 报错

* [ ] `python manager.py --cli` 在无 tkinter 环境可启动

* [ ] 未授权来源无法触达应用（限源 IP / Basic Auth / 隧道）

## 7. 运维巡检（可选，ops_monitor.py）

内置的服务器巡检脚本，每日通过 **Server酱** 把早报与高危告警推到手机微信：

| 模式 | 用途 |
| --- | --- |
| `--check` | 高危项巡检，**15 分钟一次**（cron），异常即时推送、每日去重 |
| `--report` | 每日早报：全量指标 + LLM 通俗总结（默认每天 07:30） |
| `--dry-run` | 只采集打印不推送，上线前先跑一次看效果 |
| `--test` | 发送一条测试推送，验证 SendKey |

**监控维度**：系统资源（磁盘/内存/负载）、systemd 服务与 HTTP 探活、模型调用失败率（解析 app.log）、安全审计（auth.log SSH 爆破、nginx 扫描特征与 401 暴破、fail2ban 状态）、TLS 证书有效期。

**配置**（.env，勿入库）：

```bash
SERVERCHAN_SENDKEY=SCTxxxxxxxx            # sct.ftqq.com 免费注册，每天 5 条
# 可选：OPS_DISK_WARN=80、OPS_SSH_CRIT=100 … 阈值见脚本 THRESHOLDS
```

**cron 安装**（deploy 用户）：

```bash
crontab -e
# PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# */15 * * * * cd /opt/wordisle/mvp && venv/bin/python ops_monitor.py --check >> /opt/wordisle/logs/ops_check.log 2>&1
# 30 7  * * * cd /opt/wordisle/mvp && venv/bin/python ops_monitor.py --report >> /opt/wordisle/logs/ops_report.log 2>&1
```

**注意**：公网服务器 SSH 爆破高发，建议安装 fail2ban 并启用 sshd jail 自动封禁；巡检脚本可直接读取 `/var/log/auth.log` 统计爆破（journald 收不到 syslog 路径的 sshd 日志）。

