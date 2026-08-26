"""反馈看板（跨用户库聚合）回归测试。

覆盖：
  1. 游客访问 /api/admin/* → 403，开发者/管理员 → 200
  2. dashboard：跨库聚合反馈数 / 生成数 / 满意度 / 活跃度序列
  3. history：全站生成记录分页、关键词 / 反馈过滤
  4. 数据隔离不受影响：游客仍只能看自己的 /api/feedback

运行：cd mvp && python -m unittest test_admin -v
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import auth as auth_module
import db as db_module
import main
import routes as routes_module


def _seed(uid: str, generations=(), feedback=()):
    """向指定用户库写入生成记录与反馈（模拟真实业务数据）。"""
    conn = sqlite3.connect(str(db_module._user_db_path(uid)))
    conn.row_factory = sqlite3.Row
    for gid, gtype, title in generations:
        conn.execute(
            "INSERT OR REPLACE INTO generations (id, words, generation_type, story_title, story_synopsis, body_en, created_at)"
            " VALUES (?,?,?,?,?,?, datetime('now','localtime'))",
            (gid, json.dumps(["monitor"]), gtype, title, title + " synopsis", title + " body"),
        )
    for gid, rating in feedback:
        conn.execute(
            "INSERT OR IGNORE INTO feedback (generation_id, rating) VALUES (?,?)", (gid, rating),
        )
    conn.commit()
    conn.close()


def _seed_usage(uid: str, rows=()):
    """写入 model_usage 记录（模拟模型调用）。rows: [(category, model, detail, tokens)]。"""
    conn = sqlite3.connect(str(db_module._user_db_path(uid)))
    for cat, model, detail, tokens in rows:
        conn.execute(
            "INSERT INTO model_usage (category, model, detail, tokens) VALUES (?,?,?,?)",
            (cat, model, detail, tokens),
        )
    conn.commit()
    conn.close()


class AdminDashboardTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_path = Path(cls._tmp.name)
        os.environ["MIGRATE_LEGACY_DB"] = "0"
        auth_module.AUTH_DISABLED = False
        auth_module.DEV_USERNAME = "dev"
        auth_module.DEV_PASSWORD = "dev-pass"
        auth_module.ADMIN_USERS = [("admin1", "admin1-pass")]
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

    def _ensure_guest_data_seeded(self):
        """登录一次游客（建库）并播种游客数据，返回游客 uid。"""
        self.client.post("/api/login-guest")
        guest_uid = None
        for p in sorted(db_module.USER_DATA_DIR.glob("guest-*.db")):
            guest_uid = p.stem
        if guest_uid:
            _seed(guest_uid, generations=[("guest-gen-1", "single", "游客故事")],
                  feedback=[("guest-gen-1", "up"), ("guest-gen-1", "down")])
            _seed_usage(guest_uid, [("tts", "cosyvoice-v3-flash", "单词发音", 100)])
        return guest_uid

    def setUp(self):
        self._client_cm = TestClient(main.app)
        self.client = self._client_cm.__enter__()
        # 开发者登录（建库）→ 播种开发者数据；游客登录（建库）→ 播种游客数据
        self.client.post("/api/login", json={"username": "dev", "password": "dev-pass"})
        _seed("dev", generations=[("dev-gen-1", "batch", "开发者故事")],
              feedback=[("dev-gen-1", "up")])
        _seed_usage("dev", [("llm", "deepseek-chat", "批量编译·剧情生成", 500),
                            ("llm", "bailian-qwen3.7-flash", "批量编译·文生图提示词", 300)])
        self.client.post("/api/logout")
        self._ensure_guest_data_seeded()
        self.client.post("/api/logout")

    def tearDown(self):
        self._client_cm.__exit__(None, None, None)

    def login_dev(self):
        self.client.post("/api/login", json={"username": "dev", "password": "dev-pass"})

    # ---------------- 权限 ----------------

    def test_guest_forbidden_from_admin_api(self):
        self.client.post("/api/logout")
        self.client.post("/api/login-guest")
        self.assertEqual(self.client.get("/api/admin/dashboard").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/history").status_code, 403)

    def test_admin_allowed(self):
        self.client.post("/api/logout")
        self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        self.assertEqual(self.client.get("/api/admin/dashboard").status_code, 200)

    # ---------------- dashboard 聚合正确性 ----------------

    def test_dashboard_aggregates_cross_user_stats(self):
        self.login_dev()
        data = self.client.get("/api/admin/dashboard").json()
        self.assertGreaterEqual(data["stats"]["users_total"], 3)  # dev + admin1 + guest
        # 跨库：dev 1 条 batch + guest 1 条 single
        self.assertGreaterEqual(data["stats"]["generations_total"], 2)
        fb = data["stats"]["feedback"]
        self.assertGreaterEqual(fb["up"], 2)    # dev up + guest up
        self.assertGreaterEqual(fb["down"], 1)  # guest down
        self.assertGreater(fb["satisfaction"], 0)
        types = {t["type"]: t["cnt"] for t in data["type_dist"]}
        self.assertGreaterEqual(types.get("batch", 0), 1)
        self.assertGreaterEqual(types.get("single", 0), 1)
        labels = {t["type"]: t["label"] for t in data["type_dist"]}
        self.assertEqual(labels.get("batch"), "批量编译")
        # 最近反馈明细应包含游客的 down
        fd = [r for r in data["recent_feedback"] if r["rating"] == "down"]
        self.assertTrue(fd)
        self.assertTrue(any(r["role"] == "guest" for r in fd))

    def test_dashboard_users_list_includes_role(self):
        self.login_dev()
        users = self.client.get("/api/admin/dashboard").json()["users"]
        roles = {u["role"] for u in users}
        self.assertIn("dev", roles)
        self.assertIn("admin", roles)
        self.assertIn("guest", roles)

    # ---------------- history ----------------

    def test_admin_history_lists_all_users(self):
        self.login_dev()
        data = self.client.get("/api/admin/history").json()
        self.assertGreaterEqual(data["total"], 2)
        usernames = {r["username"] for r in data["rows"]}
        self.assertIn("dev", usernames)
        self.assertTrue(any(u.startswith("guest-") for u in usernames))

    def test_admin_history_filters(self):
        self.login_dev()
        all_down = self.client.get("/api/admin/history", params={"rating": "down"}).json()
        self.assertGreaterEqual(all_down["total"], 1)
        self.assertTrue(all(r["fb_down"] > 0 for r in all_down["rows"]))
        by_role = self.client.get("/api/admin/history", params={"role": "guest"}).json()
        self.assertTrue(all(r["role"] == "guest" for r in by_role["rows"]))
        by_q = self.client.get("/api/admin/history", params={"q": "游客故事"}).json()
        # 多轮 setUp 会累积多个同名"游客故事"记录，只断言过滤生效且结果全部匹配
        self.assertGreaterEqual(by_q["total"], 1)
        self.assertTrue(all("游客故事" in (r["story_title"] or "") for r in by_q["rows"]))

    # ---------------- 不破坏原有隔离 ----------------

    def test_guest_feedback_still_own_db_only(self):
        self.client.post("/api/logout")
        self.client.post("/api/login-guest")
        stats = self.client.get("/api/feedback").json()
        # 新游客库无反馈（旧游客库数据不共享）
        self.assertEqual(stats["total"], 0)

    # ---------------- 用量情况（dev/admin 全站聚合 + 用户字段 + 排行榜） ----------------

    def test_usage_platform_aggregates_users_rank(self):
        self.login_dev()
        d = self.client.get("/api/usage").json()
        self.assertIn("users_rank", d)
        rank = {r["username"]: r for r in d["users_rank"]}
        self.assertIn("dev", rank)
        self.assertGreaterEqual(rank["dev"]["calls"], 2)          # dev 2 次 llm
        self.assertGreaterEqual(rank["dev"]["per"]["llm"]["calls"], 2)
        # recent 明细带用户身份
        recent = d.get("recent", [])
        self.assertTrue(any(r.get("username") == "dev" for r in recent))
        self.assertTrue(any(r.get("username", "").startswith("guest-") for r in recent))
        # 汇总 = 跨库总和（dev 2×llm + guest 1×tts）
        self.assertGreaterEqual(d["summary"]["calls"]["llm"], 2)
        self.assertGreaterEqual(d["summary"]["calls"]["tts"], 1)

    def test_usage_guest_has_no_platform_rank(self):
        self.client.post("/api/logout")
        self.client.post("/api/login-guest")
        d = self.client.get("/api/usage").json()
        self.assertNotIn("users_rank", d)  # 游客只看到自己的用量，不见全站排行


if __name__ == "__main__":
    unittest.main()