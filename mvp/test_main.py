"""main.py 回归测试（unittest + TestClient，无额外依赖）。

覆盖：
  1. /api/texts/{id} 系列接口应接受字符串 id（generations.id 为 uuid 字符串）
  2. voice/speed 参数校验（防路径遍历与非法值）
  3. 剧情连环画模式：build_user_prompt 注入画面数与主题；/api/generate 返回 panels

运行：cd mvp && python -m unittest test_main -v
"""
import json
import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

import auth as auth_module
import db as db_module
import main
import routes as routes_module
import services as services_module

FAKE_PANEL = {
    "scene_index": 1,
    "scene_role": "setup",
    "sentence_en": "We can accommodate the needs and negotiate the terms.",
    "sentence_zh": "我们可以满足需求并协商条款。",
    "target_words_in_scene": ["accommodate", "negotiate"],
    "word_notes": {"accommodate": "容纳；满足", "negotiate": "谈判；协商"},
    "collocations": ["accommodate the needs", "negotiate terms"],
    "image_prompt": "Cinematic storyboard: test scene.",
}

FAKE_RESULT = {
    "story_title": "Test Story",
    "theme": "测试主题",
    "story_synopsis": "测试剧情简介",
    "ending_moral": "测试寓意",
    "panels": [FAKE_PANEL],
    "included_words": ["accommodate", "negotiate"],
    "missing_words": [],
    "polysemy_notes": {"accommodate": "商务语境中为满足、容纳（需求）"},
}


def _seed_generation(gen_id="abc12345", body_en="We can accommodate the needs."):
    conn = sqlite3.connect(str(main.DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO generations (id, words, body_en) VALUES (?,?,?)",
        (gen_id, json.dumps(["accommodate"]), body_en),
    )
    conn.commit()
    conn.close()


def _seed_tts_usage(tts_count=1, ai_count=0):
    conn = sqlite3.connect(str(main.DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO daily_usage (day, ai_count, tts_count) VALUES (?,?,?)",
        (date.today().isoformat(), ai_count, tts_count),
    )
    conn.commit()
    conn.close()


def _mock_image_failure():
    """文生图 mock：全部失败（不落盘、不调外部 API）。"""
    return mock.patch.object(
        routes_module, "generate_panel_image",
        new=mock.AsyncMock(return_value={"url": None, "file_name": None, "error": "mock 图片失败"}),
    )


def _mock_image_success():
    """文生图 mock：全部成功。"""
    return mock.patch.object(
        routes_module, "generate_panel_image",
        new=mock.AsyncMock(return_value={"url": "/images/x.png", "file_name": "x.png", "error": None}),
    )


def _patch_test_paths(cls):
    """把业务库/系统库/媒体目录全部指向临时目录，并关闭认证与旧库迁移。
    业务库对齐 get_db("dev") 的实际路径（USER_DATA_DIR/dev-wordisle.db）。"""
    os.environ["MIGRATE_LEGACY_DB"] = "0"
    auth_module.AUTH_DISABLED = True
    main.DB_PATH = db_module.DB_PATH = cls._tmp_path / "dev-wordisle.db"
    auth_module.SYSTEM_DB_PATH = routes_module.SYSTEM_DB_PATH = db_module.SYSTEM_DB_PATH = cls._tmp_path / "system.db"
    db_module.USER_DATA_DIR = cls._tmp_path
    main.AUDIOS_DIR = db_module.AUDIOS_DIR = routes_module.AUDIOS_DIR = cls._tmp_path / "audios"
    db_module.AUDIOS_DIR.mkdir(exist_ok=True)
    main.VIDEOS_DIR = routes_module.VIDEOS_DIR = cls._tmp_path / "videos"
    routes_module.VIDEOS_DIR.mkdir(exist_ok=True)


class MainAppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_path = Path(cls._tmp.name)
        _patch_test_paths(cls)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self._client_cm = TestClient(main.app)
        self.client = self._client_cm.__enter__()
        conn = sqlite3.connect(str(main.DB_PATH), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute("DELETE FROM daily_usage")
            conn.execute("DELETE FROM generations")
            conn.execute("DELETE FROM audios")
            conn.execute("DELETE FROM videos")
            conn.execute("DELETE FROM word_root_links")
            conn.execute("DELETE FROM word_structures")
            conn.execute("DELETE FROM word_roots")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self._client_cm.__exit__(None, None, None)

    # ================= Bug #1: /api/texts/{id} 应接受字符串 id =================

    def test_get_text_accepts_string_id(self):
        _seed_generation()
        r = self.client.get("/api/texts/abc12345")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], "abc12345")

    def test_favorite_accepts_string_id(self):
        _seed_generation()
        r = self.client.post("/api/texts/abc12345/favorite", json={"favorited": True})
        self.assertEqual(r.status_code, 200)

    def test_delete_text_accepts_string_id(self):
        _seed_generation()
        r = self.client.delete("/api/texts/abc12345")
        self.assertEqual(r.status_code, 200)

    def test_regenerate_audio_accepts_string_id(self):
        _seed_generation()
        async def fake_tts(text, voice=None, speed=1.0, tts_model=None, feature=None):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r = self.client.post(
                "/api/texts/abc12345/regenerate-audio",
                json={"voice": "loongandy_v3", "speed": 1.0, "tts_model": "cosyvoice-v3-flash"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["url"], "/audios/abc12345_loongandy_v3_100_cosyvoice-v3-flash.mp3")
        self.assertTrue((main.AUDIOS_DIR / "abc12345_loongandy_v3_100_cosyvoice-v3-flash.mp3").exists())

    # ================= Bug #4: voice/speed 参数校验 =================

    def test_audio_rejects_path_traversal_voice(self):
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0, feature=None):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r = self.client.post(
                "/api/generations/abc12345/audio",
                json={"voice": "../../evil", "speed": 1.0},
            )
        self.assertEqual(r.status_code, 400)
        self.assertFalse((self._tmp_path / "evil_100.mp3").exists())

    def test_audio_rejects_speed_out_of_range(self):
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0, feature=None):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r = self.client.post(
                "/api/generations/abc12345/audio",
                json={"voice": "loongandy_v3", "speed": 5.0},
            )
        self.assertEqual(r.status_code, 400)

    def test_audio_accepts_valid_voice_and_speed(self):
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0, tts_model=None, feature=None):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r = self.client.post(
                "/api/generations/abc12345/audio",
                json={"voice": "loongandy_v3", "speed": 1.5, "tts_model": "cosyvoice-v3-flash"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["url"], "/audios/abc12345_loongandy_v3_150_cosyvoice-v3-flash.mp3")

    # ================= #8: 列表 body_en 预览不应错误追加"..." =================

    def test_list_generations_short_body_no_ellipsis(self):
        _seed_generation(body_en="Short body.")
        r = self.client.get("/api/generations")
        self.assertEqual(r.status_code, 200)
        item = next(i for i in r.json() if i["id"] == "abc12345")
        self.assertEqual(item["body_en"], "Short body.")
        self.assertFalse(item["body_en"].endswith("..."))

    def test_list_generations_null_body_en_no_crash(self):
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            conn.execute(
                "INSERT INTO generations (id, words, body_en) VALUES (?,?,?)",
                ("abc12345", "[]", ""),
            )
            conn.commit()
        finally:
            conn.close()
        r = self.client.get("/api/generations")
        self.assertEqual(r.status_code, 200)
        item = next(i for i in r.json() if i["id"] == "abc12345")
        self.assertEqual(item["body_en"], "")

    # ================= #9: get_generation words 字段空值兜底 =================

    def test_get_generation_empty_words_no_crash(self):
        conn = sqlite3.connect(str(main.DB_PATH))
        conn.execute("INSERT INTO generations (id, words, body_en) VALUES (?,?,?)", ("abc12345", "", "body"))
        conn.commit()
        conn.close()
        r = self.client.get("/api/generations/abc12345")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["words"], [])

    # ================= #10: 剧情连环画 prompt 注入画面数与主题 =================

    def test_build_prompt_injects_panel_count_and_theme(self):
        p = main.build_user_prompt(["accommodate"], panel_count=5, theme_hint="投资失败")
        self.assertIn("5", p)
        self.assertIn("投资失败", p)

    def test_build_prompt_defaults(self):
        p = main.build_user_prompt(["accommodate"])
        self.assertIn("4", p)  # 默认画面数

    # ================= #16: 用量统计应原子累加、并发不丢 =================

    def test_consume_quota_atomic_no_overcount(self):
        _seed_tts_usage(tts_count=0, ai_count=0)
        results = []

        def worker():
            results.append(main.consume_daily_quota("ai"))

        threads = [threading.Thread(target=worker) for _ in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 不再设上限：60 次调用全部记录成功
        self.assertEqual(results.count(True), 60)
        self.assertEqual(results.count(False), 0)
        conn = sqlite3.connect(str(main.DB_PATH))
        row = conn.execute("SELECT ai_count FROM daily_usage WHERE day=?", (date.today().isoformat(),)).fetchone()
        conn.close()
        self.assertEqual(row[0], 60)

    # ================= #13: 重新生成音频应去重复用 =================

    def test_regenerate_audio_dedup_reuses_existing(self):
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0, tts_model=None, feature=None):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r1 = self.client.post(
                "/api/texts/abc12345/regenerate-audio",
                json={"voice": "loongandy_v3", "speed": 1.0, "tts_model": "cosyvoice-v3-flash"},
            )
            r2 = self.client.post(
                "/api/texts/abc12345/regenerate-audio",
                json={"voice": "loongandy_v3", "speed": 1.0, "tts_model": "cosyvoice-v3-flash"},
            )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertTrue(r2.json().get("cached"))
        self.assertEqual(len(list(main.AUDIOS_DIR.glob("abc12345_loongandy_v3_100_cosyvoice-v3-flash.mp3"))), 1)

    # ================= #11: /api/health 应返回每日用量（含文生图） =================

    def test_health_returns_daily_usage(self):
        _seed_tts_usage(tts_count=3, ai_count=2)
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        du = r.json()["daily_usage"]
        self.assertEqual(du["ai"], 2)
        self.assertEqual(du["tts"], 3)
        self.assertEqual(du["image"], 0)

    # ================= #17: /api/image-models 返回多档模型阶梯 =================

    def test_image_models_returns_three_tiers(self):
        r = self.client.get("/api/image-models")
        self.assertEqual(r.status_code, 200)
        models = r.json()["models"]
        tiers = [m["tier"] for m in models]
        # 各档位均存在
        for t in ["旗舰", "高清", "万相", "性价比", "免费"]:
            self.assertIn(t, tiers)
        # 模型数量（5个百炼 + 2个 TokenRhythm 免费 = 7）
        self.assertEqual(len(models), 7)
        values = [m["value"] for m in models]
        self.assertIn("wan2.7-image-free", values)
        self.assertIn("qwen-image-3.0-pro", values)
        self.assertIn("wan2.7-image", values)
        # 模型 value 唯一（前端 v-for :key 依赖）
        self.assertEqual(len(values), len(set(values)))

    # ================= #18: /api/generate 返回剧情连环画结构 =================

    def test_generate_returns_panels(self):
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style="", track=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             _mock_image_success():
            r = self.client.post(
                "/api/generate",
                json={"words": "accommodate", "panel_count": 3, "theme_hint": "投资失败",
                      "image_model": "z-image-turbo", "generate_audio_immediately": False},
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["story_title"], "Test Story")
        self.assertEqual(data["panel_count"], len(data["panels"]))  # panel_count 返回实际画面数（M10）
        self.assertEqual(data["image_model"], "z-image-turbo")
        self.assertEqual(len(data["panels"]), 1)
        self.assertEqual(data["image_success_count"], 1)

    def test_generate_image_fail_fast(self):
        """fail-fast：任一文生图失败，整体返回失败并提示更换模型，不落库。"""
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style="", track=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             _mock_image_failure():
            r = self.client.post(
                "/api/generate",
                json={"words": "accommodate", "panel_count": 3, "image_model": "z-image-turbo"},
            )
        self.assertEqual(r.status_code, 502)
        self.assertIn("更换文生图模型", r.json()["detail"])
        # 失败即中止：不写入 generation 记录
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            n = conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_generate_rejects_missing_image_model(self):
        """去掉兜底：未选文生图模型必须明确报错。"""
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style="", track=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             _mock_image_success():
            r = self.client.post("/api/generate", json={"words": "accommodate"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("选择文生图模型", r.json()["detail"])

    def test_generate_stream_emits_steps_and_result(self):
        """SSE 流式：应产出 step 事件（含模型名）与 result 事件（含最终结果）。"""
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style="", track=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             _mock_image_success():
            r = self.client.post("/api/generate-stream", json={
                "words": "accommodate", "panel_count": 3, "image_model": "z-image-turbo",
            })
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        body = r.text
        self.assertIn("event: step", body)
        self.assertIn("event: result", body)
        self.assertIn("z-image-turbo", body)  # 步骤中应体现实际调用的文生图模型
        self.assertIn("Test Story", body)     # result 中包含最终结果

    def test_words_restore_restores_deleted_word(self):
        """撤销删除：restore 接口应把被删单词重新插入（保留词性/释义）。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        conn.execute("INSERT INTO words (word, pos, meaning_zh) VALUES (?,?,?)", ("restoreme", "v.", "恢复"))
        wid = conn.execute("SELECT id FROM words WHERE word='restoreme'").fetchone()[0]
        conn.commit(); conn.close()

        r = self.client.delete(f"/api/words/{wid}")
        self.assertEqual(r.status_code, 200)
        conn = sqlite3.connect(str(main.DB_PATH))
        gone = conn.execute("SELECT COUNT(*) c FROM words WHERE word='restoreme'").fetchone()[0]
        conn.close()
        self.assertEqual(gone, 0)

        r = self.client.post("/api/words/restore", json={"words": [{"word": "restoreme", "pos": "v.", "meaning_zh": "恢复"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["restored"], 1)
        conn = sqlite3.connect(str(main.DB_PATH))
        row = conn.execute("SELECT pos, meaning_zh FROM words WHERE word='restoreme'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "v.")

    def test_import_stream_emits_steps_and_result(self):
        """导入 SSE：入库 + AI 补全应产出 step 与 result 事件。"""
        async def fake_enrichment(batch):
            return {"skipped": False, "results": [{"word": batch[0], "pos": "n.", "meaning_zh": "测试释义"}]}

        with mock.patch.object(routes_module, "call_word_enrichment", fake_enrichment):
            r = self.client.post("/api/words/import-stream", json={"words": ["applepie"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        self.assertIn("event: step", r.text)
        self.assertIn("event: result", r.text)
        self.assertIn('"imported": 1', r.text)          # 导入成功 1 个
        self.assertIn("AI 补充词性释义", r.text)          # 补全步骤存在

    # ================= 构词拆解 / 全局频率 =================

    def _seed_morpheme(self, word, root="-age", root_zh="表行为/费用", root_type="suffix",
                       source="scan", freq="", meaning="", structure="word + -age", in_words=True):
        """测试助手：造一条词根树记录（词→结构→词根→关联）。返回 root_id。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            if in_words:
                conn.execute(
                    "INSERT OR IGNORE INTO words (word, frequency_level, frequency_source) VALUES (?,?, 'llm')",
                    (word, freq),
                )
            conn.execute(
                "INSERT OR IGNORE INTO word_structures (word, structure_code, is_decomposable, model) VALUES (?,?,1,'mock')",
                (word, structure),
            )
            r = conn.execute("SELECT id FROM word_roots WHERE root=?", (root,)).fetchone()
            if r:
                root_id = r[0]
            else:
                cur = conn.execute("INSERT INTO word_roots (root, root_zh, root_type) VALUES (?,?,?)", (root, root_zh, root_type))
                root_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO word_root_links (word, root_id, source, frequency_level, meaning_zh) VALUES (?,?,?,?,?)",
                (word, root_id, source, freq if source == "seed" else "", meaning if source == "seed" else ""),
            )
            conn.commit()
            return root_id
        finally:
            conn.close()

    def test_morpheme_tables_and_words_frequency_columns(self):
        """初始化应建三张构词表，且 words 表具备全局频率两列。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in ("word_roots", "word_structures", "word_root_links"):
                self.assertIn(t, tables)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(words)").fetchall()]
            self.assertIn("frequency_level", cols)
            self.assertIn("frequency_source", cols)
        finally:
            conn.close()

    def test_morpheme_detect_stream_creates_roots(self):
        """构词拆解 SSE：命中启发式粗筛的词 → LLM 判定 → 建词根树入库。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        for w in ("brokerage", "storage"):
            conn.execute("INSERT OR IGNORE INTO words (word) VALUES (?)", (w,))
        conn.commit()
        conn.close()

        async def fake_detect(words):
            return {"results": [
                {"word": "brokerage", "is_decomposable": True, "stem": "broker", "stem_zh": "经纪人",
                 "affixes": [{"affix": "-age", "type": "suffix", "meaning": "表行为/费用"}],
                 "structure_code": "broker + -age", "root": "-age", "root_zh": "表行为/费用", "root_type": "suffix",
                 "word_family": ["broker", "brokerage"]},
                {"word": "storage", "is_decomposable": True, "stem": "store", "stem_zh": "存储",
                 "affixes": [{"affix": "-age", "type": "suffix", "meaning": "表行为"}],
                 "structure_code": "store + -age", "root": "-age", "root_zh": "表行为/费用", "root_type": "suffix",
                 "word_family": ["store", "storage"]},
            ], "model": "mock-llm"}

        with mock.patch.object(routes_module, "call_morpheme_detect", fake_detect):
            r = self.client.post("/api/morphemes/detect-stream", json={"force": True, "limit": 50})
        self.assertEqual(r.status_code, 200)
        self.assertIn("event: step", r.text)
        self.assertIn("event: result", r.text)
        self.assertIn('"ok": true', r.text)

        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            root = conn.execute("SELECT * FROM word_roots WHERE root='-age'").fetchone()
            self.assertIsNotNone(root)
            links = conn.execute(
                "SELECT COUNT(*) c FROM word_root_links WHERE root_id=? AND source='scan'", (root[0],)
            ).fetchone()[0]
            self.assertEqual(links, 2)
            struct = conn.execute("SELECT structure_code FROM word_structures WHERE word='brokerage'").fetchone()
            self.assertEqual(struct[0], "broker + -age")
        finally:
            conn.close()

    def test_morpheme_roots_list_and_tree(self):
        """词根树列表 + 单树详情：P1 已收录排在 P2 推荐前，推荐词带频率/释义。"""
        rid = self._seed_morpheme("brokerage", root="-age", freq="", structure="broker + -age")
        self._seed_morpheme("storage", root="-age", freq="", structure="store + -age")
        self._seed_morpheme("coverage", root="-age", source="seed", freq="★★★★☆", meaning="覆盖范围",
                            structure="cover + -age", in_words=False)

        r = self.client.get("/api/morphemes/roots")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(x["root"] == "-age" for x in r.json()["items"]))

        tree = self.client.get(f"/api/morphemes/roots/{rid}").json()
        words = [m["word"] for m in tree["members"]]
        self.assertIn("brokerage", words)
        self.assertIn("coverage", words)
        self.assertLess(words.index("brokerage"), words.index("coverage"))
        cov = next(m for m in tree["members"] if m["word"] == "coverage")
        self.assertEqual(cov["frequency_level"], "★★★★☆")
        self.assertEqual(cov["meaning_zh"], "覆盖范围")

    def test_morpheme_seed_word_promoted_after_import(self):
        """「暂存→继承」：P2 推荐词导入词库后，在词根树里应升为已收录(P1)且频率读 words（不因 link 暂存清空而丢失）。"""
        rid = self._seed_morpheme("brokerage", root="-age", freq="", structure="broker + -age")
        self._seed_morpheme("coverage", root="-age", source="seed", freq="★★★★☆", meaning="覆盖范围",
                            structure="cover + -age", in_words=False)

        # 继承前：coverage 不在词库 → P2 推荐，频率读 link 暂存
        tree = self.client.get(f"/api/morphemes/roots/{rid}").json()
        before = next(m for m in tree["members"] if m["word"] == "coverage")
        self.assertEqual(before["priority"], 2)

        # 把 coverage 导入词库（走继承逻辑）
        async def fake_enrich(words):
            return {"results": [{"word": words[0], "pos": "n.", "meaning_zh": "", "frequency_level": ""}]}
        with mock.patch.object(routes_module, "call_word_enrichment", fake_enrich):
            r = self.client.post("/api/words", json={"word": "coverage"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["frequency_level"], "★★★★☆")  # 继承 link 暂存频率

        # 继承后：coverage 已在词库 → 升为 P1 已收录，频率仍显示（words 环节）
        tree = self.client.get(f"/api/morphemes/roots/{rid}").json()
        after = next(m for m in tree["members"] if m["word"] == "coverage")
        self.assertEqual(after["priority"], 1)
        self.assertEqual(after["frequency_level"], "★★★★☆")

    def test_morpheme_expand_adds_seed_words(self):
        """单树添加成员：追加 3 个 P2 推荐词，去重跳过已在树的词。"""
        rid = self._seed_morpheme("brokerage", root="-age", freq="", structure="broker + -age")

        async def fake_seed(root, root_zh, root_type, existing):
            return {"recommended": [
                {"word": "demopost", "meaning_zh": "邮资", "frequency_level": "★★★★☆"},
                {"word": "demowast", "meaning_zh": "损耗量", "frequency_level": "★★★☆☆"},
                {"word": "brokerage", "meaning_zh": "经纪费", "frequency_level": "★★★★★"},
            ], "reason": ""}

        with mock.patch.object(routes_module, "call_morpheme_seed", fake_seed):
            r = self.client.post(f"/api/morphemes/roots/{rid}/expand")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["added"]), 2)
        self.assertIn("demopost", body["added"])
        self.assertIn("brokerage", body["skipped"])

        tree = self.client.get(f"/api/morphemes/roots/{rid}").json()
        post = next(m for m in tree["members"] if m["word"] == "demopost")
        self.assertEqual(post["frequency_level"], "★★★★☆")
        self.assertEqual(post["meaning_zh"], "邮资")

    def test_morpheme_expand_skips_library_words(self):
        """添加成员时，已在词库的推荐词应跳过（不重复收录）。"""
        rid = self._seed_morpheme("brokerage", root="-age", freq="")
        conn = sqlite3.connect(str(main.DB_PATH))
        conn.execute("INSERT OR IGNORE INTO words (word) VALUES ('postage')")
        conn.commit()
        conn.close()

        async def fake_seed(root, root_zh, root_type, existing):
            return {"recommended": [{"word": "postage", "meaning_zh": "邮资", "frequency_level": "★★★★☆"}], "reason": ""}

        with mock.patch.object(routes_module, "call_morpheme_seed", fake_seed):
            r = self.client.post(f"/api/morphemes/roots/{rid}/expand")
        self.assertEqual(r.status_code, 200)
        self.assertIn("postage", r.json()["skipped"])

    def test_morpheme_words_list_query_and_delete(self):
        """已拆词列表/搜索、单词语法查询、删除（级联清理关联）。"""
        rid = self._seed_morpheme("brokerage", root="-age", freq="★★★★★", structure="broker + -age")
        conn = sqlite3.connect(str(main.DB_PATH))
        conn.execute("INSERT OR IGNORE INTO words (word) VALUES ('untouched')")
        conn.commit()
        conn.close()

        cand = self.client.get("/api/morphemes/candidates").json()
        self.assertGreaterEqual(cand["total"], 1)  # untouched 尚未判定

        r = self.client.get("/api/morphemes/words")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(x["word"] == "brokerage" for x in r.json()["items"]))

        q = self.client.get("/api/morphemes?word=brokerage")
        self.assertEqual(q.status_code, 200)
        self.assertEqual(q.json()["structure_code"], "broker + -age")
        self.assertEqual(q.json()["roots"][0]["root"], "-age")

        d = self.client.delete("/api/morphemes/words/brokerage")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["deleted"], 1)
        tree = self.client.get(f"/api/morphemes/roots/{rid}").json()
        self.assertNotIn("brokerage", [m["word"] for m in tree["members"]])

    def test_frequency_inherit_from_link_on_import(self):
        """P2 推荐词导入词库：频率/释义从 word_root_links 继承到 words，并清理暂存。"""
        self._seed_morpheme("wastage", root="-age", source="seed", freq="★★★☆☆", meaning="损耗量", in_words=False)

        async def fake_enrich(words):
            return {"results": [{"word": words[0], "pos": "n.", "meaning_zh": "损耗量", "frequency_level": ""}]}

        with mock.patch.object(routes_module, "call_word_enrichment", fake_enrich):
            r = self.client.post("/api/words", json={"word": "wastage"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["frequency_level"], "★★★☆☆")

        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            link = conn.execute(
                "SELECT frequency_level, meaning_zh FROM word_root_links WHERE word='wastage' AND source='seed'"
            ).fetchone()
            self.assertEqual((link[0], link[1]), ("", ""))   # 暂存已清理
            ws = conn.execute("SELECT frequency_level FROM words WHERE word='wastage'").fetchone()
            self.assertEqual(ws[0], "★★★☆☆")
        finally:
            conn.close()

    def test_migrate_words_frequency_from_polysemy(self):
        """初始化迁移：已入库的熟词僻意种子频率一次性并入 words（来源记 seed）。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            conn.execute("INSERT OR IGNORE INTO words (word) VALUES ('address')")
            conn.execute("INSERT OR REPLACE INTO polysemy (word, frequency_level) VALUES ('address', '★★★★★')")
            conn.commit()
        finally:
            conn.close()

        db_module.init_db()  # 幂等：重跑迁移

        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            row = conn.execute("SELECT frequency_level, frequency_source FROM words WHERE word='address'").fetchone()
            self.assertEqual(row[0], "★★★★★")
            self.assertEqual(row[1], "seed")
        finally:
            conn.close()

    def test_import_stream_writes_frequency(self):
        """导入流：AI 补充返回的频率字段应写入 words（全局同源）。"""
        async def fake_enrichment(batch):
            return {"skipped": False, "results": [
                {"word": batch[0], "pos": "n.", "meaning_zh": "测试", "frequency_level": "★★★★☆"}]}

        with mock.patch.object(routes_module, "call_word_enrichment", fake_enrichment):
            r = self.client.post("/api/words/import-stream", json={"words": ["freqword"]})
        self.assertEqual(r.status_code, 200)
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            row = conn.execute("SELECT frequency_level FROM words WHERE word='freqword'").fetchone()
            self.assertEqual(row[0], "★★★★☆")
        finally:
            conn.close()

    # ================= 构词拆解 LLM 解析健壮性（回归） =================

    @staticmethod
    def _mock_chat(payload):
        """构造返回固定 content 的 _chat_completion mock（并注入可用模型配置，脱离 .env 依赖）。"""
        async def fake_chat(base_url, api_key, model, payload_=None, timeout=120.0, detail=""):
            return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}], "model": "mock"}
        fake_cfg = {"value": "mock-model", "base_url": "http://x", "api_key": "k", "model": "m"}
        return [
            mock.patch.object(services_module, "_chat_completion", fake_chat),
            mock.patch.object(services_module, "get_route_llm", return_value=fake_cfg),
        ]

    def test_morpheme_detect_prompt_builder_escapes_braces(self):
        """回归：_build_morpheme_detect_prompt 是 f-string，提示里 affixes[{affix,...}] 花括号必须转义，否则 NameError。"""
        prompt = services_module._build_morpheme_detect_prompt(["brokerage"])
        self.assertIn("affixes[{affix,type,meaning}]", prompt)  # 按字面输出，不被 f-string 求值

    def test_morpheme_detect_parses_words_key(self):
        """回归：LLM 返回 {"words": [...]} 而非 {"results": [...]} 时应能解析。"""
        payload = {"words": [
            {"word": "brokerage", "is_decomposable": True, "stem": "broker", "stem_zh": "经纪人",
             "affixes": [{"affix": "-age", "type": "suffix", "meaning": "表行为/费用"}],
             "structure_code": "broker + -age", "root": "-age", "root_zh": "表行为", "root_type": "suffix",
             "word_family": ["broker", "brokerage"]},
            {"word": "delegate", "is_decomposable": False},
        ]}
        with self._mock_chat(payload)[0], self._mock_chat(payload)[1]:
            res = asyncio.run(services_module.call_morpheme_detect(["brokerage", "delegate"]))
        results = res["results"]
        self.assertEqual(len(results), 2)
        broker = next(r for r in results if r["word"] == "brokerage")
        self.assertTrue(broker["is_decomposable"])
        self.assertEqual(broker["structure_code"], "broker + -age")
        self.assertEqual(broker["affixes"][0]["affix"], "-age")
        delegate = next(r for r in results if r["word"] == "delegate")
        self.assertFalse(delegate["is_decomposable"])

    def test_morpheme_detect_parses_top_level_array(self):
        """回归：LLM 返回顶层数组 [...] 时应能解析。"""
        payload = [
            {"word": "storage", "is_decomposable": True, "stem": "store", "stem_zh": "存储",
             "affixes": [{"affix": "-age", "type": "suffix", "meaning": "表行为"}],
             "structure_code": "store + -age", "root": "-age", "root_zh": "表行为", "root_type": "suffix",
             "word_family": ["store", "storage"]},
        ]
        chat_patch, route_patch = self._mock_chat(payload)
        with chat_patch, route_patch:
            res = asyncio.run(services_module.call_morpheme_detect(["storage"]))
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["word"], "storage")

    def test_morpheme_seed_parses_words_key(self):
        """回归：词根推荐 LLM 返回 {"words": [...]} 时应能解析。"""
        payload = {"words": [
            {"word": "demopost", "meaning_zh": "邮资", "frequency_level": "★★★★☆"},
        ]}
        chat_patch, route_patch = self._mock_chat(payload)
        with chat_patch, route_patch:
            res = asyncio.run(services_module.call_morpheme_seed("-age", "表行为/费用", "suffix", ["brokerage"]))
        self.assertIsNotNone(res)
        self.assertEqual(len(res["recommended"]), 1)
        self.assertEqual(res["recommended"][0]["word"], "demopost")

    # ================= P1-2: 切换 TTS 模型重新生成，音频文件不得互相覆盖 =================

    def test_regenerate_audio_different_models_distinct_files(self):
        """同一记录用两个 TTS 模型生成音频：文件名应包含模型名，两个文件并存且内容互不覆盖。"""
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0, tts_model=None, feature=None):
            return f"mp3-of-{tts_model}".encode()

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r1 = self.client.post(
                "/api/texts/abc12345/regenerate-audio",
                json={"voice": "loongandy_v3", "speed": 1.0, "tts_model": "cosyvoice-v3-flash"},
            )
            r2 = self.client.post(
                "/api/texts/abc12345/regenerate-audio",
                json={"voice": "loongandy_v3", "speed": 1.0, "tts_model": "qwen-audio-3.0-tts-plus"},
            )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertNotEqual(r1.json()["file_name"], r2.json()["file_name"])
        f1 = main.AUDIOS_DIR / r1.json()["file_name"]
        f2 = main.AUDIOS_DIR / r2.json()["file_name"]
        self.assertTrue(f1.exists())
        self.assertTrue(f2.exists())
        self.assertNotEqual(f1.read_bytes(), f2.read_bytes())

    # ================= P1-1 / P2-1: 视频编译流程回归 =================

    def test_video_script_failure_marks_video_failed(self):
        """P2-1 回归：视频脚本 LLM 失败时，videos 记录应更新为 failed 而非永久停留 pending。"""
        async def fake_script(words, theme_hint="", art_style="", track=""):
            raise HTTPException(502, "脚本生成失败(mock)")

        with mock.patch.object(routes_module, "call_video_script", fake_script):
            r = self.client.post(
                "/api/video/generate",
                json={"words": "accommodate", "video_model": "wan2.2-t2v-plus"},
            )
        self.assertEqual(r.status_code, 502)
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            rows = conn.execute("SELECT status, error FROM videos").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "failed")
        self.assertIn("脚本生成失败", rows[0][1])

    def test_video_generate_completes_with_mux(self):
        """P1-1 回归：mux 移入线程池后，视频编译全流程（脚本→视频→TTS→mux）参数透传与完成状态不变。"""
        async def fake_script(words, theme_hint="", art_style="", track=""):
            return {
                "story_title": "Test Video", "narration_en": "Hello world.",
                "narration_zh": "你好", "video_prompt": "a video prompt",
                "included_words": [], "missing_words": [],
            }, {}

        async def fake_video(prompt, model, duration=5):
            return b"fake-mp4"

        async def fake_tts(text, voice=None, speed=1.0, model=None, feature=None):
            return b"fake-mp3"

        mux_calls = []

        def fake_mux(video_path, audio_bytes, subtitle_text, output_path):
            mux_calls.append((video_path, audio_bytes, subtitle_text, output_path))
            Path(output_path).write_bytes(b"final-video")

        with mock.patch.object(routes_module, "call_video_script", fake_script), \
             mock.patch.object(routes_module, "call_video_generation", fake_video), \
             mock.patch.object(routes_module, "call_tts", fake_tts), \
             mock.patch.object(routes_module, "mux_video_with_audio", fake_mux):
            r = self.client.post(
                "/api/video/generate",
                json={"words": "accommodate", "video_model": "wan2.2-t2v-plus",
                      "tts_model": "cosyvoice-v3-flash"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "success")
        self.assertEqual(len(mux_calls), 1)
        self.assertEqual(mux_calls[0][1], b"fake-mp3")      # 音频字节透传
        self.assertEqual(mux_calls[0][2], "Hello world.")   # 字幕文本透传
        # 原始视频临时文件已被清理
        self.assertEqual(len(list(routes_module.VIDEOS_DIR.glob("*_raw.mp4"))), 0)

    # ================= P2-2: 熟词僻意热词按实星数排序 =================

    def test_polysemy_hot_orders_by_star_count(self):
        """P2-2 回归：热词排序应按实星(★)数量降序，★★★★★ 排在 ★★★★☆ 之前。"""
        # 种子数据已移除（产品定位：一切源自词库），测试自造数据
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            for w, f in [("alpha", "★★★☆☆"), ("beta", "★★★★★"), ("gamma", "★★★★☆"), ("delta", "★☆☆☆☆")]:
                conn.execute("INSERT OR IGNORE INTO polysemy (word, frequency_level) VALUES (?,?)", (w, f))
            conn.commit()
        finally:
            conn.close()
        r = self.client.get("/api/polysemy/hot?page=1")
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        self.assertTrue(items)
        stars = [it["frequency_level"].count("★") for it in items]
        self.assertEqual(stars, sorted(stars, reverse=True))

    # ================= Phase B: 数据治理（LLM 输出 → 入库防线 + 一键清理） =================

    def test_is_clean_ai_word_heuristics(self):
        """入库防线：黑名单词、非法字符、乱码词应拒绝；正常词放行。"""
        self.assertFalse(db_module.is_clean_ai_word("inplicate"))      # 黑名单测试残留
        self.assertFalse(db_module.is_clean_ai_word("deprecated"))     # 黑名单技术词
        self.assertFalse(db_module.is_clean_ai_word("abc!!xyz"))       # 含非法字符
        self.assertFalse(db_module.is_clean_ai_word("lllllll"))        # 连续重复字母乱码
        self.assertFalse(db_module.is_clean_ai_word("a"))              # 太短
        self.assertTrue(db_module.is_clean_ai_word("accommodate"))
        self.assertTrue(db_module.is_clean_ai_word("delegate"))

    def test_clean_meaning_residue_strips_meta(self):
        """释义残留清理：剥离"（技术语境…）"会话注释，保留真实释义；正常释义不动。"""
        self.assertEqual(
            db_module.clean_meaning_residue("耗尽；使枯竭（技术语境中常指资源/内存/配额被耗尽）"),
            "耗尽；使枯竭",
        )
        # administer 的"（政策、测试等）"是合法商务释义，不应误清
        self.assertEqual(
            db_module.clean_meaning_residue("管理，经营；执行，实施（政策、测试等）；给予，施用"),
            "管理，经营；执行，实施（政策、测试等）；给予，施用",
        )
        self.assertEqual(db_module.clean_meaning_residue("正常释义，无需清理"), "正常释义，无需清理")

    def test_clean_suspicious_endpoint(self):
        """一键清理：开发者调用应删除黑名单测试词并剥离释义残留。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            conn.execute("DELETE FROM words WHERE word IN ('inplicate','meeting','deplete','administer')")
            conn.execute("INSERT INTO words (word, meaning_zh) VALUES ('inplicate', '（数学）内摆线；（几何）内旋轮线')")
            conn.execute("INSERT INTO words (word, meaning_zh) VALUES ('meeting', '')")
            conn.execute("INSERT INTO words (word, meaning_zh) VALUES ('deplete', '耗尽；使枯竭（技术语境中常指资源/内存/配额被耗尽）')")
            conn.execute("INSERT INTO words (word, meaning_zh) VALUES ('administer', '管理，经营；执行，实施（政策、测试等）')")
            conn.commit()
        finally:
            conn.close()
        r = self.client.post("/api/polysemy/clean-suspicious")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["deleted_count"], 2)          # inplicate + meeting
        self.assertIn("inplicate", body["deleted"])
        self.assertEqual(body["fixed_count"], 1)            # deplete
        self.assertIn("deplete", body["fixed"])
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            gone = conn.execute("SELECT COUNT(*) c FROM words WHERE word IN ('inplicate','meeting')").fetchone()[0]
            self.assertEqual(gone, 0)
            deplete = conn.execute("SELECT meaning_zh FROM words WHERE word='deplete'").fetchone()
            self.assertEqual(deplete[0], "耗尽；使枯竭")
            admin = conn.execute("SELECT meaning_zh FROM words WHERE word='administer'").fetchone()
            self.assertEqual(admin[0], "管理，经营；执行，实施（政策、测试等）")   # 合法释义不动
        finally:
            conn.close()


def _seed_review_single_gen(gen_id, word, image_url="/images/a.png", scene_en="He showed a clear preference for the design."):
    """种一张 single 卡（带完整 panels 素材），供复习队列反查。"""
    panel = {
        "scene_index": 1,
        "collocation": {"phrase_en": f"give {word} to", "phrase_zh": "优先考虑", "collocation_type": "verb + noun"},
        "scene_sentence": {"en": scene_en, "zh": "他明确表达了对这款设计的偏爱。", "mood": "得意"},
        "image_prompt": "test",
        "hook_type": "夸张场景",
        "image_url": image_url,
        "derivatives": [],
    }
    conn = sqlite3.connect(str(main.DB_PATH))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO generations (id, words, panel_count, panels, generation_type, created_at) "
            "VALUES (?,?,1,?,'single', datetime('now','localtime'))",
            (gen_id, json.dumps([word]), json.dumps([panel], ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


class ReviewApiTestCase(unittest.TestCase):
    """记忆测试（独立测试页）API：import / due / answer / stats / quiz。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_path = Path(cls._tmp.name)
        _patch_test_paths(cls)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self._client_cm = TestClient(main.app)
        self.client = self._client_cm.__enter__()
        conn = sqlite3.connect(str(main.DB_PATH), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute("DELETE FROM review_schedule")
            conn.execute("DELETE FROM review_log")
            conn.execute("DELETE FROM generations")
            conn.execute("DELETE FROM words")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self._client_cm.__exit__(None, None, None)

    # ================= import：迁移落库与去重 =================

    def test_review_import_inserts_new_word_with_material(self):
        _seed_review_single_gen("gen1", "preference")
        r = self.client.post("/api/review/import", json={
            "items": [{"word": "Preference", "last_result": "forgot", "updated_at": "2026-08-20T10:00:00"}]
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["imported"], 1)
        conn = sqlite3.connect(str(main.DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM review_schedule WHERE word='preference'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["box"], 0)          # 导入入盒0
            self.assertEqual(row["generation_id"], "gen1")  # 按 word 反查素材卡
            self.assertGreater(row["next_review_at"], datetime.now().isoformat(timespec="seconds"))
        finally:
            conn.close()

    def test_review_import_dedup_keeps_existing_progress(self):
        self.client.post("/api/review/import", json={"items": [{"word": "preference", "last_result": "forgot"}]})
        # 第二次导入：已有进度则跳过
        r = self.client.post("/api/review/import", json={"items": [{"word": "preference", "last_result": "forgot"}]})
        self.assertEqual(r.json()["skipped"], 1)
        self.assertEqual(r.json()["imported"], 0)

    def test_review_import_rejects_bad_body(self):
        self.assertEqual(self.client.post("/api/review/import", json={"items": "x"}).status_code, 400)
        # 非法词条（清洗后为空）计入 invalid，不报错
        r = self.client.post("/api/review/import", json={"items": [{"word": "!!!"}, "notdict"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["invalid"], 2)

    def test_review_import_without_card_leaves_generation_empty(self):
        """反查无卡：generation_id 置空，仍入库（后续只出匹配/挖空题）。"""
        r = self.client.post("/api/review/import", json={"items": [{"word": "ghostword", "last_result": "forgot"}]})
        self.assertEqual(r.json()["imported"], 1)
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            row = conn.execute("SELECT generation_id FROM review_schedule WHERE word='ghostword'").fetchone()
            self.assertEqual(row[0], "")
        finally:
            conn.close()

    # ================= due：到期判定 / 上限截断 / 素材容错 =================

    def _make_due(self, word, hours_ago=1):
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            past = (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT OR REPLACE INTO review_schedule (word, generation_id, box, next_review_at) VALUES (?,?,1,?)",
                (word, "", past),
            )
            conn.commit()
        finally:
            conn.close()

    def test_review_due_returns_material_and_repairs_dangling_generation(self):
        _seed_review_single_gen("gen1", "preference")
        self._make_due("preference")
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            conn.execute("INSERT OR REPLACE INTO words (word, meaning_zh) VALUES ('preference','偏爱；优先权')")
            conn.commit()
        finally:
            conn.close()
        r = self.client.get("/api/review/due")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["total_due"], 1)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["word"], "preference")
        self.assertEqual(item["meaning_zh"], "偏爱；优先权")
        self.assertTrue(item["has_image"])
        self.assertEqual(item["image_url"], "/images/a.png")
        # 悬挂 generation_id（空串）被反查修复为 gen1
        self.assertEqual(item["generation_id"], "gen1")

    def test_review_due_daily_limit_truncation(self):
        # 25 个纯字母假词（复习词形清洗只保留字母/连字符/撇号）
        words = [f"dueword{chr(ord('a') + i)}" for i in range(25)]
        for i, w in enumerate(words):
            self._make_due(w, hours_ago=i + 1)
        r = self.client.get("/api/review/due")
        data = r.json()
        self.assertEqual(data["total_due"], 25)
        self.assertEqual(len(data["items"]), 20)  # 每日上限 20
        # 最老的优先：next_review_at 升序
        self.assertEqual(data["items"][0]["word"], words[24])
        # 已答额度扣减：答 5 题后剩余额度 15
        for w in words[:5]:
            self.client.post("/api/review/answer", json={"word": w, "correct": True})
        r2 = self.client.get("/api/review/due")
        # 前 5 个词已答对排到未来，不再到期；额度也消耗了 5
        self.assertEqual(r2.json()["total_due"], 20)
        self.assertEqual(len(r2.json()["items"]), 15)
        # override_limit 跳过截断
        r3 = self.client.get("/api/review/due?override_limit=true")
        self.assertEqual(len(r3.json()["items"]), 20)

    def test_review_due_empty_when_nothing_due(self):
        self.client.post("/api/review/import", json={"items": [{"word": "preference", "last_result": "forgot"}]})
        r = self.client.get("/api/review/due")
        self.assertEqual(r.json()["total_due"], 0)
        self.assertEqual(r.json()["items"], [])

    # ================= answer：Leitner 状态转移 + review_log =================

    def _answer(self, word, correct, pre_box=None):
        if pre_box is not None:
            conn = sqlite3.connect(str(main.DB_PATH))
            try:
                conn.execute("UPDATE review_schedule SET box=? WHERE word=?", (pre_box, word))
                conn.commit()
            finally:
                conn.close()
        return self.client.post("/api/review/answer", json={"word": word, "correct": correct, "question_type": "image_recall"})

    def test_answer_transitions(self):
        self.client.post("/api/review/import", json={"items": [{"word": "preference", "last_result": "forgot"}]})
        # 盒0 答对 → 盒1、次日
        r = self._answer("preference", True, pre_box=0)
        j = r.json()
        self.assertEqual(j["box"], 1)
        self.assertAlmostEqual(
            datetime.fromisoformat(j["next_review_at"]), datetime.now() + timedelta(days=1), delta=timedelta(seconds=5)
        )
        # 盒1 答对 → 盒2、3 天后
        j = self._answer("preference", True, pre_box=1).json()
        self.assertEqual(j["box"], 2)
        self.assertAlmostEqual(
            datetime.fromisoformat(j["next_review_at"]), datetime.now() + timedelta(days=3), delta=timedelta(seconds=5)
        )
        # 盒2 答错 → 盒1、次日
        j = self._answer("preference", False, pre_box=2).json()
        self.assertEqual(j["box"], 1)
        self.assertEqual(j["lapses"], 1)
        # 盒3 答对 → 盒4（已掌握）、30 天后
        j = self._answer("preference", True, pre_box=3).json()
        self.assertEqual(j["box"], 4)
        self.assertAlmostEqual(
            datetime.fromisoformat(j["next_review_at"]), datetime.now() + timedelta(days=30), delta=timedelta(seconds=5)
        )
        # 盒4 答对 → 保持盒4、再续 30 天
        j = self._answer("preference", True, pre_box=4).json()
        self.assertEqual(j["box"], 4)
        # 盒4 答错 → 回盒1
        j = self._answer("preference", False, pre_box=4).json()
        self.assertEqual(j["box"], 1)

    def test_answer_writes_review_log(self):
        self.client.post("/api/review/import", json={"items": [{"word": "preference", "last_result": "forgot"}]})
        self._answer("preference", True)
        conn = sqlite3.connect(str(main.DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM review_log").fetchone()
            self.assertEqual(row["word"], "preference")
            self.assertEqual(row["result"], "correct")
            self.assertEqual(row["question_type"], "image_recall")
        finally:
            conn.close()

    def test_answer_404_for_unknown_word(self):
        self.assertEqual(self.client.post("/api/review/answer", json={"word": "nope", "correct": True}).status_code, 404)

    # ================= stats：盒子分布 / 正确率 / streak =================

    def test_stats_streak_and_accuracy(self):
        self.client.post("/api/review/import", json={"items": [{"word": "preference", "last_result": "forgot"}]})
        self._answer("preference", True)
        # 补两条历史日志：昨天、前天（连续 3 天）
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            for d in (1, 2):
                past = (datetime.now() - timedelta(days=d)).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT INTO review_log (word, result, question_type, answered_at) VALUES (?,?,?,?)",
                    ("preference", "wrong", "cloze", past),
                )
            conn.commit()
        finally:
            conn.close()
        r = self.client.get("/api/review/stats").json()
        self.assertEqual(r["streak"], 3)
        self.assertEqual(r["answered_total"], 3)
        self.assertEqual(r["correct_total"], 1)
        self.assertAlmostEqual(r["accuracy"], 1 / 3, places=3)
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["in_progress"], 1)
        self.assertEqual(r["mastered"], 0)

    def test_stats_streak_not_broken_before_today(self):
        """今天未作答、昨天作答过：streak 从昨天起算（不算断）。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            past = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO review_log (word, result, question_type, answered_at) VALUES ('w','correct','cloze',?)",
                (past,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.client.get("/api/review/stats").json()["streak"], 1)

    # ================= quiz：题型生成 / 干扰项 / 素材降级 =================

    def test_quiz_generates_three_types_with_options(self):
        # 三个词：preference 有图有中文；nograph 无图；w1-w4 补干扰项池
        _seed_review_single_gen("gen1", "preference")
        _seed_review_single_gen("gen2", "nograph", image_url="")
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            for w in ("w1", "w2", "w3", "w4"):
                conn.execute("INSERT OR REPLACE INTO words (word, meaning_zh) VALUES (?,?)", (w, f"释义{w}"))
            conn.commit()
        finally:
            conn.close()
        self._make_due("preference")
        self._make_due("nograph")
        r = self.client.get("/api/review/quiz?count=2")
        self.assertEqual(r.status_code, 200)
        questions = r.json()["questions"]
        self.assertGreaterEqual(len(questions), 1)
        for q in questions:
            self.assertIn(q["word"], ("preference", "nograph"))
            if q["type"] in ("image_recall", "match"):
                # 看图选义 / 中英匹配：正确项中文义（卡词伙优先）在选项中且去重
                self.assertEqual(q["correct_zh"], "优先考虑")
                self.assertTrue(q["correct_zh"])
                self.assertIn(q["correct_zh"], q["options"])
                self.assertEqual(len(q["options"]), len(set(q["options"])))  # 选项去重
            elif q["type"] == "cloze":
                self.assertIn("____", q["sentence_masked"])

    def test_quiz_degrades_no_image_to_match(self):
        _seed_review_single_gen("gen2", "nograph", image_url="")
        self._make_due("nograph")
        questions = self.client.get("/api/review/quiz?count=1&types=image_recall").json()["questions"]
        # 无图：看图说词降级为 match（有 phrase_zh）
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["type"], "match")

    def test_quiz_empty_when_no_queue(self):
        r = self.client.get("/api/review/quiz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["questions"], [])
        self.assertEqual(r.json()["total_words"], 0)

    def test_quiz_skips_word_without_any_material(self):
        """无卡无释义的词：三种题型素材全缺，跳过不出题。"""
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            conn.execute(
                "INSERT INTO review_schedule (word, generation_id, box, next_review_at) "
                "VALUES ('bare','','1',datetime('now','localtime','-1 hour'))"
            )
            conn.commit()
        finally:
            conn.close()
        questions = self.client.get("/api/review/quiz?count=5").json()["questions"]
        self.assertEqual([q for q in questions if q["word"] == "bare"], [])


class FontAndFFmpegPickTest(unittest.TestCase):
    """Linux 部署适配：跨平台字体挑选与 ffmpeg 选择策略（设计文档 3.2.1 / 3.2.3）。"""

    def test_pick_font_win32_hits_arial(self):
        def exists(p):
            return p == r"C:\Windows\Fonts\arial.ttf"
        with mock.patch("services.sys.platform", "win32"), \
             mock.patch("os.path.exists", side_effect=exists):
            self.assertEqual(services_module._pick_font(), "C:/Windows/Fonts/arial.ttf")

    def test_pick_font_linux_hit_and_missing_all(self):
        def exists(p):
            return p == "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        with mock.patch("services.sys.platform", "linux"), \
             mock.patch("os.path.exists", side_effect=exists):
            self.assertEqual(services_module._pick_font(),
                             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        with mock.patch("services.sys.platform", "linux"), \
             mock.patch("os.path.exists", return_value=False):
            self.assertEqual(services_module._pick_font(), "")

    def test_pick_ffmpeg_prefers_system_with_drawtext(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("services._ffmpeg_has_drawtext", return_value=True):
            self.assertEqual(services_module._pick_ffmpeg_exe(), "/usr/bin/ffmpeg")

    def test_pick_ffmpeg_falls_back_to_imageio(self):
        fake = mock.Mock(get_ffmpeg_exe=lambda: "/opt/imageio/ffmpeg")
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.dict(sys.modules, {"imageio_ffmpeg": fake}):
            self.assertEqual(services_module._pick_ffmpeg_exe(), "/opt/imageio/ffmpeg")

    def test_pick_ffmpeg_falls_back_when_system_has_no_drawtext(self):
        fake = mock.Mock(get_ffmpeg_exe=lambda: "/opt/imageio/ffmpeg")
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("services._ffmpeg_has_drawtext", return_value=False), \
             mock.patch.dict(sys.modules, {"imageio_ffmpeg": fake}):
            self.assertEqual(services_module._pick_ffmpeg_exe(), "/opt/imageio/ffmpeg")

    def test_pick_ffmpeg_none_available_returns_bare_command(self):
        real_import = __import__
        def guarded_import(name, *args, **kwargs):
            if name == "imageio_ffmpeg":
                raise ImportError("blocked")
            return real_import(name, *args, **kwargs)
        with mock.patch("shutil.which", return_value=None), \
             mock.patch("builtins.__import__", side_effect=guarded_import):
            self.assertEqual(services_module._pick_ffmpeg_exe(), "ffmpeg")


if __name__ == "__main__":
    unittest.main()
