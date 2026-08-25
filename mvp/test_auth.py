"""用户系统（认证 + 每用户分库 + 每日配额）回归测试。

覆盖：
  1. 未登录访问 /api/* → 401；/api/health 放行
  2. 游客免密登录 → 会话 Cookie → /api/me 返回身份与配额
  3. 开发者/管理员账号登录（含错误密码 401、退出登录）
  4. 游客配额：同步端点 5 次后用尽 429；流式端点同样受配额约束
  5. 数据隔离：流式端点（SSE 生成器内 get_db）写入游客库而非开发者库
  6. 开发者/管理员不限量

运行：cd mvp && python -m unittest test_auth -v
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import auth as auth_module
import db as db_module
import main
import routes as routes_module


class AuthTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_path = Path(cls._tmp.name)
        os.environ["MIGRATE_LEGACY_DB"] = "0"
        # 开启认证 + 播种开发者/管理员测试账号（密码存于测试代码，非生产）
        auth_module.AUTH_DISABLED = False
        auth_module.DEV_USERNAME = "dev"
        auth_module.DEV_PASSWORD = "dev-pass"
        auth_module.ADMIN_USERS = [("admin1", "admin1-pass")]
        # 全部库/媒体目录指向临时目录
        main.DB_PATH = db_module.DB_PATH = cls._tmp_path / "dev-wordisle.db"
        auth_module.SYSTEM_DB_PATH = routes_module.SYSTEM_DB_PATH = db_module.SYSTEM_DB_PATH = cls._tmp_path / "system.db"
        db_module.USER_DATA_DIR = cls._tmp_path
        main.AUDIOS_DIR = db_module.AUDIOS_DIR = routes_module.AUDIOS_DIR = cls._tmp_path / "audios"
        db_module.AUDIOS_DIR.mkdir(exist_ok=True)
        main.VIDEOS_DIR = routes_module.VIDEOS_DIR = cls._tmp_path / "videos"
        routes_module.VIDEOS_DIR.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self._client_cm = TestClient(main.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self):
        self._client_cm.__exit__(None, None, None)

    # ---------------- 认证基本 ----------------

    def test_unauthenticated_api_returns_401(self):
        self.assertEqual(self.client.get("/api/words").status_code, 401)

    def test_health_is_public(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_login_page_available(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("游客直接进入", r.text)

    def test_guest_login_sets_cookie_and_me(self):
        r = self.client.post("/api/login-guest")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.cookies.get("wordisle_session"))
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        data = me.json()
        self.assertEqual(data["role"], "guest")
        self.assertTrue(data["uid"].startswith("guest-"))
        self.assertEqual(data["limits"]["video"]["limit"], 2)
        self.assertEqual(data["limits"]["batch"]["limit"], 5)

    def test_login_bad_password_401(self):
        r = self.client.post("/api/login", json={"username": "dev", "password": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_login_dev_ok_and_unlimited(self):
        r = self.client.post("/api/login", json={"username": "dev", "password": "dev-pass"})
        self.assertEqual(r.status_code, 200)
        me = self.client.get("/api/me").json()
        self.assertEqual(me["role"], "dev")
        self.assertEqual(me["limits"]["batch"]["limit"], -1)  # 不限量

    def test_login_admin_ok(self):
        r = self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/me").json()["role"], "admin")

    def test_logout_clears_session(self):
        self.client.post("/api/login-guest")
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/words").status_code, 401)

    # ---------------- 配额 ----------------

    def _fake_deepseek_empty_panels(self):
        async def fake_deepseek(words, panel_count=4, theme_hint="", style="", art_style="", track=""):
            return {"story_title": "T", "panels": [], "included_words": [], "missing_words": []}, {}
        return mock.patch.object(routes_module, "call_deepseek", fake_deepseek)

    def test_guest_batch_quota_enforced_sync(self):
        """游客 batch 配额 5 次：前 5 次进入流程（面板空→502），第 6 次 429。"""
        self.client.post("/api/login-guest")
        with self._fake_deepseek_empty_panels():
            for i in range(5):
                r = self.client.post(
                    "/api/generate",
                    json={"words": "accommodate", "panel_count": 3, "image_model": "z-image-turbo",
                          "generate_audio_immediately": False},
                )
                self.assertEqual(r.status_code, 502, f"第{i + 1}次应进入流程")
            r6 = self.client.post(
                "/api/generate",
                json={"words": "accommodate", "panel_count": 3, "image_model": "z-image-turbo"},
            )
        self.assertEqual(r6.status_code, 429)
        self.assertIn("上限", r6.json()["detail"])

    def test_guest_stream_quota_and_isolation(self):
        """流式端点：enrich 配额 5 次后 429；且词写入游客库而非开发者库（contextvar 跨流生效）。"""
        self.client.post("/api/login-guest")
        guest_uid = self.client.get("/api/me").json()["uid"]
        with mock.patch.object(routes_module, "_ensure_word_audio", new=mock.AsyncMock(return_value="")):
            for i in range(1, 6):
                r = self.client.post(
                    "/api/words/single-stream",
                    json={"word": f"streamword{i}", "pos": "n.", "meaning_zh": "测试", "frequency_level": "★★★★☆"},
                )
                self.assertEqual(r.status_code, 200, f"第{i}次应成功")
            # 第 6 次超限
            r = self.client.post(
                "/api/words/single-stream",
                json={"word": "streamword6", "pos": "n.", "meaning_zh": "x"},
            )
        self.assertEqual(r.status_code, 429)
        self.assertIn("上限", r.json()["detail"])
        # 隔离：5 个词都在游客库
        conn = sqlite3.connect(str(db_module._user_db_path(guest_uid)))
        try:
            n = conn.execute("SELECT COUNT(*) c FROM words WHERE word LIKE 'streamword%'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 5)
        # 开发者库无这 5 个词
        conn = sqlite3.connect(str(db_module._user_db_path(auth_module.DEV_USERNAME)))
        try:
            n = conn.execute("SELECT COUNT(*) c FROM words WHERE word LIKE 'streamword%'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_me_reflects_quota_used(self):
        self.client.post("/api/login-guest")
        with mock.patch.object(routes_module, "_ensure_word_audio", new=mock.AsyncMock(return_value="")):
            for i in range(3):
                r = self.client.post(
                    "/api/words/single-stream",
                    json={"word": f"qw{i}", "pos": "n.", "meaning_zh": "x", "frequency_level": "★★★☆☆"},
                )
                self.assertEqual(r.status_code, 200)
        me = self.client.get("/api/me").json()
        self.assertEqual(me["limits"]["enrich"]["used"], 3)
        self.assertEqual(me["limits"]["enrich"]["remaining"], 2)

    def test_dev_unlimited_quota(self):
        """开发者批量编译不限量：连发 7 次（> 游客 5 次）仍不被 429。"""
        self.client.post("/api/login", json={"username": "dev", "password": "dev-pass"})
        with self._fake_deepseek_empty_panels():
            for i in range(7):
                r = self.client.post(
                    "/api/generate",
                    json={"words": "accommodate", "panel_count": 3, "image_model": "z-image-turbo",
                          "generate_audio_immediately": False},
                )
                self.assertEqual(r.status_code, 502, f"第{i + 1}次不应被配额拦截（dev 不限量）")


if __name__ == "__main__":
    unittest.main()
