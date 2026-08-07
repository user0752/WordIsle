# TOEIC MVP 个人版

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
