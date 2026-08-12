"""main.py 回归测试（unittest + TestClient，无额外依赖）。

覆盖：
  1. /api/texts/{id} 系列接口应接受字符串 id（generations.id 为 uuid 字符串）
  2. TTS/AI 每日限额必须生效（生成接口立即音频、regenerate-audio、新的一天限额为 0）
  3. voice/speed 参数校验（防路径遍历与非法值）
  4. 剧情连环画模式：build_user_prompt 注入画面数与主题；/api/generate 返回 panels

运行：cd mvp && python -m unittest test_main -v
"""
import json
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
        async def fake_tts(text, voice=None, speed=1.0, tts_model=None):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r = self.client.post(
                "/api/texts/abc12345/regenerate-audio",
                json={"voice": "loongandy_v3", "speed": 1.0},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["url"], "/audios/abc12345_loongandy_v3_100.mp3")
        self.assertTrue((main.AUDIOS_DIR / "abc12345_loongandy_v3_100.mp3").exists())

    # ================= Bug #2: 每日限额必须生效 =================

    def test_generate_respects_ai_limit_zero(self):
        async def fake_deepseek(words, panel_count=4, theme_hint="", style=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             mock.patch.object(db_module, "DAILY_AI_LIMIT", 0):
            r = self.client.post("/api/generate", json={"words": "accommodate"})
        self.assertEqual(r.status_code, 429)

    def test_generate_immediate_audio_respects_tts_limit(self):
        _seed_tts_usage(tts_count=1)

        async def fake_deepseek(words, panel_count=4, theme_hint="", style=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        tts_calls = []

        async def fake_tts(text, voice=None, speed=1.0):
            tts_calls.append(1)
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             mock.patch.object(routes_module, "call_tts", fake_tts), \
             mock.patch.object(db_module, "DAILY_TTS_LIMIT", 1), \
             _mock_image_failure():
            r = self.client.post(
                "/api/generate",
                json={"words": "accommodate", "generate_audio_immediately": True},
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["has_audio"])
        self.assertIn("audio_error", data)
        self.assertEqual(tts_calls, [])

    def test_regenerate_audio_respects_tts_limit(self):
        _seed_generation()
        _seed_tts_usage(tts_count=1)

        async def fake_tts(text, voice=None, speed=1.0):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts), \
             mock.patch.object(db_module, "DAILY_TTS_LIMIT", 1):
            r = self.client.post("/api/texts/abc12345/regenerate-audio", json={})
        self.assertEqual(r.status_code, 429)

    # ================= Bug #4: voice/speed 参数校验 =================

    def test_audio_rejects_path_traversal_voice(self):
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0):
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

        async def fake_tts(text, voice=None, speed=1.0):
            return b"fake-mp3"

        with mock.patch.object(routes_module, "call_tts", fake_tts):
            r = self.client.post(
                "/api/generations/abc12345/audio",
                json={"voice": "loongandy_v3", "speed": 5.0},
            )
        self.assertEqual(r.status_code, 400)

    def test_audio_accepts_valid_voice_and_speed(self):
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0, tts_model=None):
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

    # ================= #16: 每日限额应原子占用、并发不超限 =================

    def test_consume_quota_atomic_no_overcount(self):
        _seed_tts_usage(tts_count=0, ai_count=0)
        results = []

        def worker():
            results.append(main.consume_daily_quota("ai"))

        with mock.patch.object(db_module, "DAILY_AI_LIMIT", 30):
            threads = [threading.Thread(target=worker) for _ in range(60)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(results.count(True), 30)
        self.assertEqual(results.count(False), 30)
        conn = sqlite3.connect(str(main.DB_PATH))
        row = conn.execute("SELECT ai_count FROM daily_usage WHERE day=?", (date.today().isoformat(),)).fetchone()
        conn.close()
        self.assertEqual(row[0], 30)

    # ================= #13: 重新生成音频应去重复用 =================

    def test_regenerate_audio_dedup_reuses_existing(self):
        _seed_generation()

        async def fake_tts(text, voice=None, speed=1.0, tts_model=None):
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
        self.assertEqual(du["ai_limit"], main.DAILY_AI_LIMIT)
        self.assertEqual(du["tts_limit"], main.DAILY_TTS_LIMIT)
        self.assertEqual(du["image_limit"], main.DAILY_IMAGE_LIMIT)

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
        async def fake_deepseek(words, panel_count=4, theme_hint="", style=""):
            return dict(FAKE_RESULT), {"total_tokens": 5}

        with mock.patch.object(routes_module, "call_deepseek", fake_deepseek), \
             _mock_image_failure():
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
        self.assertIn("image_error", data["panels"][0])  # 图片降级不阻塞整体
        self.assertEqual(data["image_success_count"], 0)


if __name__ == "__main__":
    unittest.main()
