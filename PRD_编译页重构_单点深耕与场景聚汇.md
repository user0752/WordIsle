# TOEIC MVP 编译页重构 PRD

> 将现有「编译」页面重构为 **「单点深耕」** 与 **「场景聚汇」** 两个独立编译入口。
> 原有单页多词故事编译功能保留，归入"批量编译"作为第三入口。

---

## 1. 概述

### 1.1 背景

当前系统只有一个编译入口（`/` 首页的生成表单），用户输入词列表 → DeepSeek 生成电影分镜故事 → 文生图 + TTS。这套流程适合批量处理 3-15 个目标词，但存在以下局限：

- 粒度单一：小量（1 个词精加工）和大量（全词库场景化）都没有入口。
- TOEIC 特有的「词伙 Collocations」（如 `submit a proposal` / `address an issue`）没有被系统性地编入学习流程。
- 单词库持续增长后缺少横向组织能力——词与词之间的关系（同场景、同领域）零散，用户需要自己想象联系。

### 1.2 目标

| 原    | 重构后 | 说明 |
|---|---|---|
| 1 个「编译」页 | **「单点深耕」**（Page 1） | 1 个词的精加工：词伙搭配 + 荒诞画面 + 派生词 |
| | **「场景聚汇」**（Page 2） | 自动检测词库→按商务场景聚类，含词伙/例句 |
| | **「批量编译」**（Page 3） | 多词批量编译，提供「荒诞三连弹」和「冲突连环」两种风格可选 |

### 1.3 原则

- **重构而非新增**：原有的编译 UI 和路由名不动或少动，两个新页面各自独立。
- **共用基础能力**：文生图、TTS、DeepSeek 调用逻辑从 `services.py` 复用。
- **数据库向前兼容**：`generations` 表通过新增 `generation_type` 字段区分三页面产出。

---

## 2. 现有编译页分析

### 2.1 当前流程

```
用户输入词列表（逗号分隔）
  ↓ POST /api/generate
参数：words[], panel_count(3|4|5), theme_hint, image_model, generate_audio_immediately
  ↓ call_deepseek() → 返回 JSON {story_title, panels[], polysemy_notes, ...}
  ↓ 并发生成图片（每 panel 一张）
  ↓ 可选 TTS
  ↓ 入库 generations 表 + words 表
  ↓ 返回详情（含 panels、images、audio）
```

### 2.2 保留 & 拆出

| 内容 | 归属 | 说明 |
|---|---|---|
| 多词电影故事生成 | **批量编译** | 保留，字段不动，`generation_type='batch'` |
| 单篇阅读 / 音频 / 历史 | 共用 | 两个新页面各自生成，共享 `audios` / `images` / 历史列表 |
| 文生图 / TTS 基础能力 | 共用 | `services.py` 不变，新 API 直接调用已有函数 |
| 熟词僻意检测 | 共用 | `polysemy` 表和 API 不变 |

---

## 3. 页面一：单点深耕

### 3.1 功能描述

**一句话**：用户从单词库选 1 个词 → LLM 生成"词伙搭配 + 荒诞场景句 + 派生词族 + 一幅记忆钩子图"。

核心理念：**一张图 + 一句话记住一个词**。图像必须是"把字面义和商务义强行塞进一个画面制造荒诞冲突"，不是自然叙事。

### 3.2 交互流程

```
[首页 / 单点深耕]
    ↓
① 从单词库选择 1 个目标词（搜索 / 下拉）
    ↓ 可选手动输入新词
② [可选] 填写主题偏好（如"finance / law / shipping"）
    ↓
③ 点击「深耕」
    ↓ POST /api/single/compile
后端流程：
    1. DeepSeek 生成 [词伙搭配 + 场景句(中英) + 派生词]
    2. 文生图（1 张），prompt 强制嵌入字面义+商务义冲突
    3. [可选] TTS 合成场景句音频
    ↓
④ 展示结果卡片：
    ┌─────────────────────────────────────┐
    │  🖼 1 张记忆钩子图                    │
    │                                      │
    │  📝 词伙搭配     submit a proposal    │
    │     中文释义     提交提案             │
    │                                      │
    │  💬 场景句(en)   He submitted a      │
    │                    $2M proposal to   │
    │                    the board.         │
    │     场景句(zh)   他向董事会提交了一份  │
    │                  200万美元的提案。    │
    │                                      │
    │  🌳 派生词族      submission (n.)    │
    │                   submissive (adj.)   │
    │                                      │
    │  🔊 播放朗读     [▶]                 │
    │  ⭐ 收藏         [★]                 │
    └─────────────────────────────────────┘
    ↓
⑤ 后续操作：加测验 / 收藏 / 再生成
```

### 3.3 输出数据模型

```json
{
  "id": "a1b2c3d4",
  "generation_type": "single",
  "word": "submit",
  "collocation": {
    "phrase_en": "submit a proposal",
    "phrase_zh": "提交提案",
    "collocation_type": "verb + noun"
  },
  "scene_sentence": {
    "en": "He submitted a $2M budget proposal to the board with visible anxiety.",
    "zh": "他带着明显的焦虑向董事会提交了一份200万美元的预算提案。",
    "mood": "荒诞 / 反差 / 紧张"
  },
  "image": {
    "url": "/images/a1b2c3d4_single.png",
    "prompt_used": "A nervous man in a suit tenderly cradling a massive proposal document like a baby, ...",
    "model": "wan2.7-image"
  },
  "derivatives": [
    { "word": "submission", "pos": "n.", "meaning_zh": "提交物；服从" },
    { "word": "submissive", "pos": "adj.", "meaning_zh": "服从的；顺从的" },
    { "word": "submittable", "pos": "adj.", "meaning_zh": "可提交的" }
  ],
  "polysemy": {
    "is_polysemy": false,
    "note": null
  },
  "audio_url": "/audios/a1b2c3d4.mp3",
  "created_at": "2026-08-11 15:30:00"
}
```

### 3.4 与 images / audios 存储的关系

- 图片：1 张，存 `{gen_id}_single.png`，路径 `/images/`。
- 音频：1 段（场景句全文），存 `{gen_id}_single.mp3`，路径 `/audios/`。
- 复用现有 `audios` 表，`generation_id` 指向单点深耕的 generation 记录。

---

## 4. 页面二：场景聚汇

### 4.1 功能描述

**一句话**：自动扫描单词库 → LLM 按商务场景把词聚类 → 每个场景展示词汇群 + 词伙搭配 + 例句。

核心理念：大脑爱分类，把零散词放进具体工作场景（HR / 财务 / 物流……），自动建立联想网络。

### 4.2 自动检测逻辑

```
触发条件（任一）：
  - 用户手动点击「重新检测」
  - 单词库总量跨过阈值（首次≥10, ≥25, ≥50, ≥100, ≥200...）
  - 导入新词后单词库净增量 ≥5 个

检测流程：
  1. 查询 words 表，获取所有词 (word 字段)
  2. 查询 scenes 表，获取已存在场景的词汇集合
  3. 去重：排除已在至少 1 个场景中归类的词 → 得到 new_words
  4. 若 new_words ≥ 3，调用 DeepSeek 场景检测 API：
     输入：new_words + 已存在场景列表
     输出：[{scene_name, scene_zh, words[], collocations[], example_sentences[]}]
  5. 写入 scenes / word_scenes / scene_collocations 表
  6. 一个单词可出现在多个场景中
```

### 4.3 场景分类体系（种子场景 + 动态扩展）

```
初始种子场景（预制，表初始化时写入）：
  HR/人事     | candidate, resume, pension, compensation, recruit, ...
  会议/活动   | agenda, venue, minutes, adjourn, convene, ...
  物流/采购   | shipment, vendor, inventory, specifications, freight, ...
  财务/办公   | revenue, quarterly, reimburse, stationery, invoice, ...
  谈判/合同   | tender, contract, negotiate, clause, breach, ...
  营销/销售   | campaign, prospect, quota, commission, launch, ...
  法务/合规   | infringe, liability, comply, regulation, patent, ...
  金融/投资   | stock, dividend, yield, bond, portfolio, asset, ...

检测中 LLM 可：
  - 将单词分配到最贴合的已有场景
  - 建议创建新场景（如单词库有 arrest、detain、warrant → 建议新增「执法/安全」场景）
```

### 4.4 交互流程

```
[首页 / 场景聚汇]
    ↓
① 页面初始化：从数据库加载已有场景列表
    ↓
┌──────────────────────────────────────────────┐
│  📊 场景聚汇    [重新检测] [导入新词]         │
│                                               │
│  ┌─ HR/人事 ──────────────────────┐           │
│  │ 5 个词  · 12 条词伙             │           │
│  │ candidate, resume, pension,     │           │
│  │ compensation, recruit            │           │
│  │ 🖼 场景封面                     │           │
│  └────────────────────────────────┘           │
│                                               │
│  ┌─ 财务/办公 ────────────────────┐           │
│  │ 4 个词  · 8 条词伙              │           │
│  │ revenue, quarterly, reimburse,  │           │
│  │ stationery                       │           │
│  └────────────────────────────────┘           │
│                                               │
│  ┌─ 新增场景建议 ──────────────────┐           │
│  │ 执法/安全 (3 个词可归入)         │           │
│  │ arrest, detain, warrant          │           │
│  │ [采纳] [忽略]                    │           │
│  └────────────────────────────────┘           │
└──────────────────────────────────────────────┘
    ↓
② 点击某个场景卡片 → 展开详情
    ↓
┌──────────────────────────────────────────────┐
│  🔙 HR/人事                                    │
│                                               │
│  📋 词汇列表 (5)                               │
│    candidate 候选人 | resume 简历              │
│    pension 养老金 | compensation 薪酬          │
│    recruit 招聘                                │
│                                               │
│  🔗 词伙搭配                                   │
│    submit a resume | offer a pension           │
│    recruit candidates | receive compensation   │
│                                               │
│  💬 场景例句（可逐句朗读）                      │
│    The HR manager reviewed all resumes...      │
│    Each candidate will receive...              │
│                                               │
│  🎬 [批量编译场景故事]  🔊 [场景全文朗读]       │
└──────────────────────────────────────────────┘
```

### 4.5 输出数据模型

```json
{
  "id": "scene_hr_1",
  "name_en": "HR & Personnel",
  "name_zh": "HR/人事",
  "description": "招聘、薪酬、福利、合同等人力资源管理相关词汇",
  "word_count": 5,
  "collocations_count": 12,
  "words": ["candidate", "resume", "pension", "compensation", "recruit"],
  "collocations": [
    { "phrase": "submit a resume", "zh": "提交简历", "words": ["submit", "resume"] },
    { "phrase": "recruit candidates", "zh": "招聘候选人", "words": ["recruit", "candidate"] }
  ],
  "example_sentences": [
    { "en": "The HR manager reviewed all resumes before the interview.", "zh": "..." }
  ],
  "cover_image_url": null,
  "created_at": "2026-08-11"
}
```

### 4.6 检测触发 API

```
POST /api/scenes/detect
参数：
  - force: boolean  (是否强制重新检测所有词，默认 false 即增量)
  - max_new_scenes: number  (单次最多建议新增场景数，默认 3)
返回：
  {
    "scenes_updated": 3,      // 已有场景更新的数量
    "words_assigned": 15,     // 新增归类的单词数
    "new_scenes_suggested": [ // 建议新增的场景
      { "name_en": "Law Enforcement", "name_zh": "执法/安全", "candidate_words": ["arrest", "detain", "warrant"] }
    ]
  }
```

### 4.7 新场景采纳

```
POST /api/scenes/adopt
参数：
  - name_en: string
  - name_zh: string
  - description: string (可选)
  - words: string[]  (初始归入词汇)
返回：
  { "ok": true, "scene_id": 10, "word_count": 3 }
```

---

## 5. 页面三：批量编译改造

### 5.1 功能描述

**一句话**：将原有多词电影故事编译升级为两种可选风格——「荒诞三连弹」和「冲突连环」，用户在编译时自行选择。

核心理念：打破"微电影叙事"框架，把 5-15 个目标词压缩进 3 个高密度场景卡。每张卡片独立可记，不依赖上下文。图像从"好看"变成"好记"——荒诞、反差、梗图风。

### 5.2 风格一：荒诞三连弹（默认）

**设计思路**：3 个独立荒诞场景，松散串联，每张图自闭环。

| 维度 | 旧（微电影） | 新（荒诞三连弹） |
|---|---|---|
| 面板数 | 4-5 个 | 固定 3 个 |
| 句子长度 | 12-25 词/句 | 8-15 词/句 |
| 英文总量 | ~80-100 词 | ~35-45 词 |
| 图像风格 | cinematic storyboard, film grain | surreal comic, absurd juxtaposition, bold colors |
| 叙事结构 | setup → development → climax → resolution | 三个独立荒诞场景，同角色或同主题松关联 |
| 核心单位 | 故事包裹词汇 | 词伙搭配 → 短梗句承载 |
| 词密度 | 2-4 词/panel | 3-5 词/panel（通过短句压密） |
| 产出字段 | story_title ✓, ending_moral ✓, scene_role ✓ | story_title ✓(仅作列表标题), collocations(每 panel), 去掉 scene_role 和 ending_moral |

**示意**（以 `submit, proposal, quarterly, revenue` 为例）：

```
Panel 1 · 荒诞场景卡 ─────────────────────────
  词伙: submit a proposal / quarterly revenue
  💬 The quarterly revenue dropped 40%, so he
     submitted a proposal written on a napkin.
  🖼 一个人在餐巾纸上狂写提案，背景是巨大的红色
     "Q3 REVENUE -40%" 投影屏幕
────────────────────────────────────────────

Panel 2 · 荒诞场景卡 ─────────────────────────
  词伙: pool resources / revenue forecast
  💬 The team pooled their resources to rebuild
     the revenue forecast using crayons.
  🖼 五个西装革履的人趴在地上用蜡笔画柱状图，
     会议室牌子写着"REVENUE WAR ROOM"
────────────────────────────────────────────

Panel 3 · 荒诞场景卡 ─────────────────────────
  词伙: submit a revised proposal / annual revenue
  💬 They submitted the crayon-drawn proposal,
     and annual revenue somehow tripled.
  🖼 董事会成员对着蜡笔提案痛哭流涕，背景屏幕
     显示 ↑300%
────────────────────────────────────────────
```

### 5.3 风格二：冲突连环

**设计思路**：两个对立角色 × 三个回合的冲突演进。利用冲突 → 情绪 → 杏仁核参与 → 更强记忆的原理。天然对齐 TOEIC 商务场景中常见的博弈/谈判语境。

| 维度 | 说明 |
|---|---|
| 面板数 | 固定 3 个（回合制） |
| 句子长度 | 10-18 词/句（比荒诞三连弹略长，需交代冲突演进） |
| 角色模型 | Panel 1: A 方出招 → Panel 2: B 方反击 → Panel 3: 荒诞结局 |
| 图像风格 | 漫画风格，强调角色表情的夸张反差，每 panel 的画面焦点在两个人的互动 |
| 冲突类型 | 由用户选择或 LLM 自动分配：买方 vs 卖方 / 老板 vs 员工 / 供应商 vs 采购 / 总部 vs 分公司 |

**示意**（以 `tender, negotiate, vendor, invoice, compromise` 为例）：

```
Panel 1 · 回合 1: A 方出招 ──────────────────
  词伙: submit a tender / select a vendor
  💬 The buyer submitted a tender, selecting
     the cheapest vendor without reading reviews.
  🖼 买家闭着眼在一堆供应商名片中随手抽了一张，
     背景是写着"TENDER"的巨型文件堆
────────────────────────────────────────────

Panel 2 · 回合 2: B 方反击 ──────────────────
  词伙: inflate an invoice / refuse to negotiate
  💬 The vendor inflated the invoice by 300%
     and refused to negotiate a single line item.
  🖼 供应商把发票举过头顶，像吹气球一样把它吹得
     越来越大，对面买家瘫在椅子上翻白眼
────────────────────────────────────────────

Panel 3 · 回合 3: 荒诞结局 ──────────────────
  词伙: reach a compromise / settle the invoice
  💬 They compromised: full payment in exchange
     for a lifetime supply of office donuts.
  🖼 两人握手，但背景是一卡车的甜甜圈正在往
     办公室倒，财务在角落里绝望地按计算器
────────────────────────────────────────────
```

### 5.4 风格选择机制

```
[批量编译页面]
    ↓
① 输入/选择目标词列表（5-15 个）
    ↓
② [可选] 填写主题偏好
    ↓
③ 选择编译风格（二选一，默认「荒诞三连弹」）：
    ○ 荒诞三连弹  独立场景卡·高密度·适合快刷
    ○ 冲突连环    角色对立·有剧情·适合深度编码
    ↓
④ 选择文生图模型 + [可选] 即时生成音频
    ↓
⑤ 点击「开始编译」
    ↓ POST /api/generate  （增加 `style` 参数）
```

### 5.5 输出数据模型

对比两风格的 `generations` 表字段差异：

| 字段 | 荒诞三连弹 (`style='absurd'`) | 冲突连环 (`style='conflict'`) |
|---|---|---|
| `generation_type` | `'batch'` | `'batch'` |
| `style` | `'absurd'` | `'conflict'` |
| `panel_count` | `3` | `3` |
| `panels[].sentence_en` | 8-15 词 | 10-18 词 |
| `panels[].collocations` | 每 panel 2-4 条词伙 | 每 panel 2-4 条词伙 |
| `panels[].scene_role` | 去掉 | 去掉（改用回合标签 `round_1/round_2/round_3`） |
| `panels[].image_prompt` | surreal comic, absurd juxtaposition | comic style, exaggerated expressions, character interaction |
| `panels[].round_label` | 无 | `'A方出招' / 'B方反击' / '荒诞结局'` |
| `story_title` | 保留（列表标题用） | 保留 |
| `ending_moral` | 去掉 | 去掉 |
| `theme` | 保留 | 保留 |

### 5.6 与旧版兼容

- 不传 `style` 参数或传旧值：后端默认使用 `'absurd'`（荒诞三连弹作为批量编译默认风格）。
- 历史 generations 数据无 `style` 字段：前端展示时视为旧版，不显示风格标签。
- `/api/generate` 路由不变，只加参数，向后兼容。

### 5.7 图像风格差异化总结

| 页面 | 图像风格 | 关键词 |
|---|---|---|
| 单点深耕 | 一个字面义+商务义冲突 | absurd duality, single image, word-meaning collision |
| 荒诞三连弹 | 独立荒诞场景，每张自闭环 | surreal comic, absurd juxtaposition, bold flat colors, weird objects |
| 冲突连环 | 两人互动冲突，表情夸张 | comic strip, character dialogue, exaggerated emotions, office satire |

---

## 6. API 汇总

### 5.1 新增路由

| 方法 | 路径 | 用途 | 消耗配额 |
|---|---|---|---|
| POST | `/api/single/compile` | 单点深耕：生成词伙+场景句+派生+图 | ai + image (各 1 次) |
| POST | `/api/single/{gen_id}/audio` | 为单点深耕场景句生成朗读音频 | tts (1 次) |
| POST | `/api/scenes/detect` | 触发场景自动检测 | ai (1 次) |
| POST | `/api/scenes/adopt` | 采纳建议的新场景 | 无 |
| GET | `/api/scenes` | 获取场景列表（概览） | 无 |
| GET | `/api/scenes/{scene_id}` | 获取单个场景详情 | 无 |
| DELETE | `/api/scenes/{scene_id}` | 删除场景及关联 | 无 |
| PATCH | `/api/scenes/{scene_id}` | 编辑场景（增删词/改描述） | 无 |
| POST | `/api/scenes/{scene_id}/compile` | 场景批量编译：把该场景所有词编成故事 | ai + image |
| GET | `/api/scenes/suggestions` | 获取待采纳的新场景建议 | 无 |

### 5.2 变更路由

| 方法 | 路径 | 变更说明 |
|---|---|---|
| POST | `/api/generate` | 加 `style` 参数（`'absurd'` / `'conflict'`），不传默认 `'absurd'`。`generation_type='batch'`（向后兼容） |
| GET | `/api/generations` | 查询参数加 `?type=single|batch|scene` 过滤，默认返回全部。batch 类结果含 `style` 字段 |
| GET | `/api/words` | 返回加 `scene_count`（该词归属的场景数） |

---

## 7. 数据库变更

### 6.1 新增表

```sql
-- scenarios 场景表
CREATE TABLE IF NOT EXISTS scenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_en TEXT NOT NULL,
    name_zh TEXT NOT NULL,
    description TEXT DEFAULT '',
    cover_image_url TEXT DEFAULT '',
    status TEXT DEFAULT 'active',   -- active | suggested | archived
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- word_scenes 词-场景关联（多对多）
CREATE TABLE IF NOT EXISTS word_scenes (
    word_id INTEGER NOT NULL,
    scene_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (word_id, scene_id),
    FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE,
    FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
);

-- scene_collocations 场景内词伙
CREATE TABLE IF NOT EXISTS scene_collocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id INTEGER NOT NULL,
    phrase_en TEXT NOT NULL,
    phrase_zh TEXT DEFAULT '',
    words TEXT DEFAULT '[]',       -- JSON: 包含的目标词列表
    example_en TEXT DEFAULT '',
    example_zh TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
);
```

### 6.2 变更表

```sql
-- generations 表加 type 字段
ALTER TABLE generations ADD COLUMN generation_type TEXT DEFAULT 'batch';
-- 可选值: 'batch' | 'single' | 'scene'
-- 旧数据默认 'batch'，向前兼容

-- generations 表加 style 字段（用于批量编译风格区分）
ALTER TABLE generations ADD COLUMN style TEXT DEFAULT '';
-- 可选值: '' | 'absurd' | 'conflict'
-- 旧数据默认 ''（视为旧版电影叙事模式），向前兼容

-- single 类型新增字段（存在 generations 表中，JSON 合并存储也行）
-- 方案A（推荐）：在 panels 字段的 JSON 中扩展，不新增列
-- 方案B（更清晰）：新增以下列
--   collocation_json TEXT DEFAULT '{}'
--   derivatives_json TEXT DEFAULT '[]'
-- 建议方案A，减少表结构变更。
```

### 6.3 种子数据

```sql
-- 初始化时插入预设场景
INSERT INTO scenes (name_en, name_zh, description) VALUES
  ('HR & Personnel',    'HR/人事',     '招聘、薪酬、福利、合同等人力资源管理'),
  ('Meeting & Events',  '会议/活动',   '会议议程、场地、纪要、休会等'),
  ('Logistics & Procurement', '物流/采购', '货运、供应商、库存、规格等'),
  ('Finance & Office',  '财务/办公',   '营收、季度、报销、文具等'),
  ('Negotiation & Contract', '谈判/合同', '投标、合同、谈判、条款等'),
  ('Marketing & Sales', '营销/销售',   '营销活动、客户、佣金、产品发布等'),
  ('Legal & Compliance', '法务/合规',  '侵权、责任、合规、监管、专利等'),
  ('Finance & Investment', '金融/投资', '股票、股息、收益率、债券、投资组合等');
```

---

## 8. LLM Prompt 设计要点

### 8.1 单点深耕 Prompt

与现有多词故事 prompt（`SYSTEM_PROMPT`）分开，新建 `SINGLE_SYSTEM_PROMPT`：

```
你是 TOEIC 商务英语教练，专攻"一个词一张图一句梗"记住单词。

## 任务
给定一个英文单词，输出：
1. 一条高频 TOEIC 词伙搭配（如 submit → submit a proposal）
2. 一句荒诞/反差/玩梗的场景句（中英双语），句中必须包含目标词
3. 一幅记忆钩子图的英文描述（image_prompt）——**必须把字的常见义和商务义塞进同一个画面制造荒诞反差**
4. 该词的派生词族（名词/动词/形容词/副词，含中英释义）

## 图像要求（关键）
- image_prompt 必须制造"两种意思的碰撞"，不是自然叙事
- 示例：tender（温柔 + 投标） → 画面描述"A person cradling a sealed tender document with exaggeratedly tender/gentle hand gestures, like holding a baby, in a cold corporate boardroom. The contrast between the gentleness and the rigid business setting creates absurdity."
- 让画面本身成为回忆线索，用户看到图就想起词的两个含义

## 输出格式
{...JSON schema...}
```

### 8.2 场景检测 Prompt

```
你是 TOEIC 词汇教学专家，负责将单词按商务场景分类。

## 任务
给定：新词列表 + 已有场景列表（每个场景含已归入词汇）。
要求：
1. 将新词分配到最贴合的已有场景（可一对多）
2. 为新场景聚类（≥3 个可归入的词）建议创建新场景
3. 对每个场景中的词，生成 2-5 条典型词伙搭配（含中文释义）

## 场景命名
使用"英文类别/中文简称"格式，如 "HR & Personnel / HR/人事"
已有场景：{existing_scenes_json}
新词：{new_words_json}

## 输出 JSON
{...}
```

### 8.3 批量编译 Prompt（两套独立 SYSTEM_PROMPT）

荒诞三连弹 (`BATCH_ABSURD_SYSTEM_PROMPT`)：

```
You are creating TOEIC vocabulary MEME CARDS — not stories, not scripts, MEME CARDS.

CORE IDEA: Pack 3-5 target words per panel into ONE absurd, punchy English sentence (8-15 words MAX). Each panel is a self-contained weird scene. Panels are LOOSELY linked (same characters or theme) but MUST be independently readable and funny on their own.

RULES:
1. EXACTLY 3 panels.
2. Every panel sentence: 8-15 words. Tight. No filler. No transitions. No "meanwhile" or "later that day".
3. Pack 3-5 target words per panel through COLLOCATIONS, not isolated words. Always list the collocations used.
   BAD: "They pooled and submitted and reported." (words stuffed in)
   GOOD: "They pooled their salaries, submitted a napkin-drawn tender, and filed the quarterly report upside down."
4. image_prompt: SURREAL/ABSURD visual metaphor. Turn the CONCEPT of the word into a bizarre physical object or situation. Flat comic style, bold colors, exaggerated expressions. NOT cinematic, NOT realistic, NO film grain, NO dramatic lighting.
   Example: For "pool resources": draw people literally dumping wallets into a swimming pool labeled "PROJECT BUDGET".
   Example: For "submit a tender": draw a giant sealed envelope being tenderly carried like a fragile baby by a businessperson.
5. Each panel outputs: collocations list (2-4 items, in English), one absurd English sentence, Chinese translation, image_prompt.
6. Output ONLY valid JSON. No markdown. No extra text.

JSON STRUCTURE:
{
  "story_title": "Short absurd title (3-6 words)",
  "theme": "Chinese theme 1-5 words",
  "style": "absurd",
  "panels": [
    {
      "scene_index": 1,
      "collocations": ["pool resources", "submit a tender"],
      "sentence_en": "They pooled their salaries and submitted a napkin-drawn tender.",
      "sentence_zh": "他们凑齐工资，提交了一份画在餐巾纸上的标书。",
      "target_words_in_scene": ["pool", "submit", "tender"],
      "image_prompt": "Surreal comic: five people in suits pouring wallets into a swimming pool labeled 'BUDGET', one holding a giant napkin with scribbled numbers above head. Flat comic style, bold flat colors, exaggerated expressions, weird humor."
    }
  ],
  "included_words": ["pool", "submit", "tender", "quarterly", "revenue"],
  "missing_words": []
}
```

冲突连环 (`BATCH_CONFLICT_SYSTEM_PROMPT`)：

```
You are creating TOEIC vocabulary COMIC STRIPS in a "conflict ping-pong" format.

CORE IDEA: Two opposing characters/business roles clash across 3 rounds. Round 1: Side A makes a move. Round 2: Side B counters. Round 3: Absurd compromise/outcome. Each round's sentence packs 3-5 target words through natural collocations.

RULES:
1. EXACTLY 3 panels, labeled round_1 / round_2 / round_3.
2. Establish two clear opposing roles at the start (buyer vs seller, boss vs employee, HQ vs branch office, vendor vs procurement, etc.).
3. Each panel sentence: 10-18 words. Slightly longer than absurd style because dialogue/conflict needs setup. But still punchy.
4. Pack 3-5 target words per panel through COLLOCATIONS.
5. image_prompt: COMIC STRIP style. Focus on the interaction between two characters. Exaggerated facial expressions, clear emotional contrast between the two sides. Office/professional setting can be stylized and silly. Flat comic style, bold colors.
   Round 1: Side A confident/smug
   Round 2: Side B angry/plotting
   Round 3: Both bewildered by the absurd outcome
6. Each panel outputs: round_label (Chinese, e.g. "买方出招"/"卖方反击"/"荒诞结局"), collocations, one English sentence, Chinese translation, image_prompt.
7. Output ONLY valid JSON. No markdown. No extra text.

JSON STRUCTURE:
{
  "story_title": "Short witty title (3-6 words)",
  "theme": "Chinese theme + conflict type",
  "style": "conflict",
  "conflict_type": "买方 vs 卖方",
  "panels": [
    {
      "scene_index": 1,
      "round_label": "买方出招",
      "collocations": ["submit a tender", "select a vendor"],
      "sentence_en": "...",
      "sentence_zh": "...",
      "target_words_in_scene": ["tender", "vendor", ...],
      "image_prompt": "Comic strip panel: [Side A character] [action] with [exaggerated expression]. [Side B visible in background reacting]. Flat comic style, bold colors."
    }
  ],
  "included_words": [...],
  "missing_words": []
}
```

---

## 9. 前端导航变更

### 9.1 改建后页面结构

```
原导航：
  [首页 / 词库 / 编译 / 历史 ...]

新导航：
  [首页 / 词库 / 单点深耕 / 场景聚汇 / 批量编译 / 历史 ...]
                          ↑ 原有编译页保留在此
```

### 9.2 批量编译页（原有编译页改造）

| 字段 | 变化 |
|---|---|
| 页面路径 | `/` 不变，或改为 `/batch` 并在首页引导至三选一 |
| `generation_type` | 入库时标 `'batch'` |
| `style` | 新增参数，`'absurd'`（荒诞三连弹，默认）/ `'conflict'`（冲突连环） |
| 前端标题 | "批量编译"替代原"编译" |
| 前端 UI | 新增风格选择开关（二选一），带简短说明和示意缩略图 |

### 9.3 首页布局建议

首页从单一「编译」表单改为三卡片入口：

```
┌─────────────────────────────────────────────────┐
│              TOEIC 顽固词深度加工               │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ 单点深耕│  │ 场景聚汇│  │ 批量编译│       │
│  │ 1个词    │  │ 按场景   │  │ 5-15词   │       │
│  │ 精加工   │  │ 自动聚类 │  │ 编故事   │       │
│  └──────────┘  └──────────┘  └──────────┘       │
│                                                  │
│  ── 最近生成 ──                                  │
│  [历史列表...]                                   │
└─────────────────────────────────────────────────┘
```

---

## 10. 迁移实施建议

### 10.1 分阶段

| 阶段 | 内容 | 影响面 |
|---|---|---|
| P1 | 数据库变更 + `single/compile` API + `services.py` 加 `call_deepseek_single()` | 最小，不影响现有功能 |
| P2 | 单点深耕前端页面 + 首页入口 | 需要改 `index.html` |
| P3 | `scenes/detect` API + 种子场景入库 | 新表，不影响现有 |
| P4 | 场景聚汇前端页面 + 场景详情 + 采纳 | 较重，主要在 `index.html` |
| P5 | 批量编译新增风格选择 + `services.py` 加 `call_deepseek_batch_absurd/conflict` + 两套新 prompt | 改 `services.py` + `index.html`，向后兼容 |
| P6 | 批量编译改名 + 首页三选一 + 历史列表过滤 | 纯 UI 重构 |

### 10.2 共享代码复用清单

| 模块 | 复用方式 |
|---|---|
| `call_tts()` | 单点深耕 + 批量编译两风格共用，传入各自生成的 `body_en` |
| `call_image_generation()` | 三页面共用，传不同的 prompt 风格 |
| `consume_daily_quota()` | 直接复用 |
| `normalize_words()` | 场景聚汇用不到，单点+批量编译复用 |
| `deepseek` 调用框架 | 新增 `call_deepseek_single()` / `call_deepseek_scene_detect()` / 改造 `call_deepseek()` 加 `style` 分派，复用 httpx 配置 |
| 文生图模型三档 (`IMAGE_MODELS`) | 三页面共用下拉 |

---

## 11. 附录：关键决策记录

| 决策点 | 选择 | 替代方案 |
|---|---|---|
| generation 表如何区分类型 | 新增 `generation_type` 列 | 拆三张表（过度设计） |
| 单点深耕的输出字段放哪 | 放在 `panels` JSON 中扩展（schemaless） | 新增专用列（后期迁移麻烦） |
| 场景检测是自动还是手动 | 手动触发 + 阈值自动化* | 纯定时（不适用本地单用户） |
| 场景内词伙的存储 | 独立表 `scene_collocations`（可复用、可检索） | 放在 scenes 的 JSON 字段里（不可检索） |
| 一张图 vs 多张图（单点深耕） | 1 张 | 多张（成本高，单点无需连环画） |
| 批量编译两风格共存 | 同一路由 + `style` 参数分派 | 拆两个路由（接口冗余） |
| 不传 style 的默认行为 | 默认 `'absurd'`（荒诞三连弹） | 默认旧版（不好，旧版已证明效果不佳） |

---

> 版本: v1.1 | 作者: WorkBuddy | 日期: 2026-08-11
