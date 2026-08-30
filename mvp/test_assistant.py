"""词小屿助手后端测试（unittest + TestClient，无额外依赖）。

覆盖：
  1. POST /api/assistant/feedback：合法 up/down 入库、重复提交同向即取消（toggle）、非法值 400
  2. db.upsert_assistant_feedback：切换语义（存 → 取消 → 再存）
  3. chat_stream 的 done 事件携带 suggests 建议追问数组（FAQ 直答路径，零 LLM、确定性）
  4. _build_suggestions：纯函数，返回 3 条互不相同的非空字符串

运行：cd mvp; ..\\venv\\Scripts\\python.exe -m unittest test_assistant -v
"""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import assistant as assistant_module
import auth as auth_module
import db as db_module
import main
from config import DEV_USERNAME


def _patch_test_paths(cls):
    """对齐 test_main 的隔离方案：业务库/系统库指向临时目录，关闭认证。"""
    os.environ["MIGRATE_LEGACY_DB"] = "0"
    auth_module.AUTH_DISABLED = True
    # 联合跑时前面模块（test_auth/test_admin）会把 auth.DEV_USERNAME 改成 "dev" 且不恢复，
    # 导致默认身份落到 dev.db；这里钉回 config 原值，保证与 main lifespan 建的库一致。
    auth_module.DEV_USERNAME = DEV_USERNAME
    main.DB_PATH = db_module.DB_PATH = cls._tmp_path / "dev-wordisle.db"
    auth_module.SYSTEM_DB_PATH = db_module.SYSTEM_DB_PATH = cls._tmp_path / "system.db"
    db_module.USER_DATA_DIR = cls._tmp_path


class _TempDBCase(unittest.TestCase):
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
        conn = sqlite3.connect(str(main.DB_PATH))
        try:
            conn.execute("DELETE FROM assistant_feedback")
            conn.execute("DELETE FROM assistant_conversations")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self._client_cm.__exit__(None, None, None)

    def _fb_rows(self) -> list[dict]:
        conn = sqlite3.connect(str(main.DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT user_id, question, answer, rating FROM assistant_feedback"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ================= 1&2. 助手回答反馈（点赞点踩，toggle 取消） =================

class AssistantFeedbackTest(_TempDBCase):

    def test_db_upsert_assistant_feedback_toggle(self):
        r1 = db_module.upsert_assistant_feedback(DEV_USERNAME, "怎么加词？", "去单词库新增。", "up")
        self.assertIsNotNone(r1)
        rows = self._fb_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rating"], "up")
        self.assertEqual(rows[0]["user_id"], DEV_USERNAME)
        # 同一条消息再次点同方向 → 取消（删除），返回 None
        r2 = db_module.upsert_assistant_feedback(DEV_USERNAME, "怎么加词？", "去单词库新增。", "up")
        self.assertIsNone(r2)
        self.assertEqual(len(self._fb_rows()), 0)
        # 取消后可再点踩（换方向不互斥）
        r3 = db_module.upsert_assistant_feedback(DEV_USERNAME, "怎么加词？", "去单词库新增。", "down")
        self.assertIsNotNone(r3)
        self.assertEqual(self._fb_rows()[0]["rating"], "down")

    def test_feedback_endpoint_up_then_toggle(self):
        payload = {"question": "怎么使用记忆测试？", "answer": "记忆测试按 Leitner 节奏安排复习。", "rating": "up"}
        r = self.client.post("/api/assistant/feedback", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertTrue(body.get("rated"))
        rows = self._fb_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question"], payload["question"])
        self.assertEqual(rows[0]["rating"], "up")
        # 重复点击同一方向 → 取消
        r2 = self.client.post("/api/assistant/feedback", json=payload)
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json().get("rated"))
        self.assertEqual(len(self._fb_rows()), 0)

    def test_feedback_endpoint_down_persists(self):
        r = self.client.post("/api/assistant/feedback", json={
            "question": "q", "answer": "a", "rating": "down"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("rated"))
        rows = self._fb_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rating"], "down")

    def test_feedback_invalid_rating_400(self):
        for bad in ("meh", ""):
            r = self.client.post("/api/assistant/feedback", json={
                "question": "q", "answer": "a", "rating": bad})
            self.assertEqual(r.status_code, 400, f"rating={bad!r} 应返回 400")
        self.assertEqual(len(self._fb_rows()), 0)


# ================= 3&4. 建议追问（done 事件携带 suggests） =================

class AssistantSuggestionsTest(_TempDBCase):

    def test_build_suggestions_basic(self):
        s = assistant_module._build_suggestions("单词库支持查词、新增与删除。")
        self.assertIsInstance(s, list)
        self.assertEqual(len(s), 3)
        for x in s:
            self.assertIsInstance(x, str)
            self.assertTrue(x.strip())
        self.assertEqual(len(set(s)), 3)

    def test_build_suggestions_with_tool(self):
        for tool in ("search_words", "get_review_due", "add_words"):
            s = assistant_module._build_suggestions("查询完成。", tool=tool)
            self.assertEqual(len(s), 3)
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in s))
            self.assertEqual(len(set(s)), 3)

    def test_chat_done_contains_suggests(self):
        """FAQ 直答路径（零 LLM）：done 事件必须带 3 条建议，且 navigate 跳转不受影响。"""
        assistant_module._faqs = [{
            "question": "怎么使用单词库？",
            "keywords": ["单词库"],
            "answer": "单词库在左侧导航，支持查词、新增、删除。",
            "related_page": "words",
        }]
        try:
            async def collect():
                out = []
                async for evt, data in assistant_module.chat_stream(DEV_USERNAME, "怎么使用单词库页面", "words"):
                    out.append((evt, data))
                return out
            events = asyncio.run(collect())
        finally:
            assistant_module._faqs = None

        dones = [d for e, d in events if e == "done"]
        self.assertEqual(len(dones), 1)
        sug = dones[0].get("suggests")
        self.assertIsInstance(sug, list, "done 事件应携带 suggests 建议数组")
        self.assertEqual(len(sug), 3)
        self.assertTrue(all(isinstance(x, str) and x.strip() for x in sug))
        tools = [d for e, d in events if e == "tool"]
        self.assertTrue(any(t.get("tool") == "navigate" for t in tools))


if __name__ == "__main__":
    unittest.main()
