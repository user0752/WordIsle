# WordIsle MVP 个人版（词屿）

最小可跑版本：FastAPI + SQLite + DeepSeek + 百炼 TTS。零 Redis/Nginx/Celery/Postgres。

## 启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 DEEPSEEK_API_KEY 和 TTS_API_KEY

# 3. 启动
python main.py

# 4. 浏览器打开
# http://localhost:8000
```

## 文件说明

```
mvp/
  main.py           # 单文件：后端 API + 嵌入式前端
  requirements.txt  # 4 个依赖
  .env.example      # API Key 模板
  data/             # 自动创建：SQLite 数据库 + mp3 音频
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 前端页面 |
| POST | `/api/generate` | 生成语境文本 |
| POST | `/api/generations/{id}/audio` | 生成听力音频 |
| GET | `/api/generations` | 历史列表 |
| GET | `/api/generations/{id}` | 单条详情 |
| DELETE | `/api/generations/{id}` | 删除记录 |
| GET | `/api/health` | 健康检查 |

## Linux 服务器部署

> 面向无桌面的 Ubuntu/Debian 服务器（2C/2GB 即可跑）。应用无鉴权且背后是付费 AI 接口，**公网放行前必须先做访问控制**（见下文），否则任何访客都能触发文生图/文生视频等付费调用。

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

- **A 限源 IP**（仅自己用）：云安全组 + `ufw` 只放行自己的公网 IP 到 8000
- **B Nginx + Basic Auth**（分享给他人）：`auth_basic` + `proxy_pass http://127.0.0.1:8000`，对外只开 80
- **C SSH 隧道**（最安全）：`ssh -N -L 8000:127.0.0.1:8000 deploy@<服务器IP>`，本地访问 `http://localhost:8000`

### 5. 无 tkinter 环境

服务器未装 GUI 时，启动管理器需以 CLI 模式运行且无 tkinter 依赖：

```bash
python manager.py --cli      # 服务端环境可直接使用
```

Windows GUI 模式不受影响。

### 6. 验证清单

- [ ] `venv/bin/python main.py` 后 `http://<IP>:8000` 可访问，首页样式正常
- [ ] 触发一次视频编译，字幕烧录无字体/ffmpeg 报错
- [ ] `python manager.py --cli` 在无 tkinter 环境可启动
- [ ] 未授权来源无法触达应用（限源 IP / Basic Auth / 隧道）
