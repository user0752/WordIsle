"""main.py 回归测试（unittest + TestClient，无额外依赖）。

覆盖：
  1. /api/texts/{id} 系列接口应接受字符串 id（generations.id 为 uuid 字符串）
  2. voice/speed 参数校验（防路径遍历与非法值）
  3. 剧情连环画模式：build_user_prompt 注入画面数与主题；/api/generate 返回 panels

运行：cd mvp && python -m unittest test_main -v
"""
import json
import asyncio
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

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


class MainAppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_path = Path(cls._tmp.name)
        main.DB_PATH = db_module.DB_PATH = cls._tmp_path / "words.db"
        main.AUDIOS_DIR = db_module.AUDIOS_DIR = routes_module.AUDIOS_DIR = cls._tmp_path / "audios"
        db_module.AUDIOS_DIR.mkdir(exist_ok=True)

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
                json={"voice": "loongandy_v3", "speed": 1.0},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["url"], "/audios/abc12345_loongandy_v3_100.mp3")
        self.assertTrue((main.AUDIOS_DIR / "abc12345_loongandy_v3_100.mp3").exists())

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
                json={"voice": "loongandy_v3", "speed": 1.5},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["url"], "/audios/abc12345_loongandy_v3_150.mp3")

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
                json={"voice": "loongandy_v3", "speed": 1.0},
            )
            r2 = self.client.post(
                "/api/texts/abc12345/regenerate-audio",
                json={"voice": "loongandy_v3", "speed": 1.0},
            )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertTrue(r2.json().get("cached"))
        self.assertEqual(len(list(main.AUDIOS_DIR.glob("abc12345_loongandy_v3_100.mp3"))), 1)

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
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style=""):
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
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style=""):
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
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             _mock_image_success():
            r = self.client.post("/api/generate", json={"words": "accommodate"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("选择文生图模型", r.json()["detail"])

    def test_generate_stream_emits_steps_and_result(self):
        """SSE 流式：应产出 step 事件（含模型名）与 result 事件（含最终结果）。"""
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style=""):
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


if __name__ == "__main__":
    unittest.main()
