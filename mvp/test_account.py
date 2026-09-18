"""账户安全（登录锁定/封禁）+ 个人中心（改密/重置/换绑/昵称）回归测试。

覆盖：
  1. 登录锁定：连续 5 次错密码 → 423 + Retry-After；递进时长；成功后清零
  2. 封禁：admin ban → 登录 403、已登录用户请求 403；unban 恢复；非 admin 调 ban 403
  3. 修改密码：旧密码错 401；改后旧密码 401、新密码可登录
  4. 短信重置密码：reset 类型码 → 重置成功、新密码可登录且自动登录 Cookie
  5. 手机号换绑：旧/新双码 → 新手机命中、旧号失效；新号被占用 409
  6. 昵称：修改后 /api/me 返回新昵称；长度非法 400
  7. users 表迁移断言：status/nickname 列存在

运行：cd mvp && python -m unittest test_account -v
"""
import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import auth as auth_module
import db as db_module
import main
import routes as routes_module
import sms as sms_module


def _seed_sms(phone, expected_type="register"):
    """直接写固定码 123456 的发送记录（与 auth 的 _hash_code 一致：sha256）。"""
    conn = sqlite3.connect(str(auth_module.SYSTEM_DB_PATH))
    try:
        conn.execute(
            "INSERT INTO sms_codes (phone, code_hash, type, expire_at, try_count, ip) "
            "VALUES (?,?,?,?,0,'test')",
            (phone, hashlib.sha256(b"123456").hexdigest(), expected_type, int(time.time()) + 300),
        )
        conn.commit()
    finally:
        conn.close()


class AccountTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_path = Path(cls._tmp.name)
        import os
        os.environ["MIGRATE_LEGACY_DB"] = "0"
        auth_module.AUTH_DISABLED = False
        auth_module.DEV_USERNAME = "dev"
        auth_module.DEV_PASSWORD = "dev-pass"
        auth_module.ADMIN_USERS = [("admin1", "admin1-pass")]
        main.DB_PATH = db_module.DB_PATH = cls._tmp_path / "dev-wordisle.db"
        auth_module.SYSTEM_DB_PATH = routes_module.SYSTEM_DB_PATH = db_module.SYSTEM_DB_PATH = cls._tmp_path / "system.db"
        db_module.USER_DATA_DIR = cls._tmp_path
        db_module._initialized_dbs.clear()
        main.AUDIOS_DIR = db_module.AUDIOS_DIR = routes_module.AUDIOS_DIR = cls._tmp_path / "audios"
        db_module.AUDIOS_DIR.mkdir(exist_ok=True)
        main.VIDEOS_DIR = routes_module.VIDEOS_DIR = cls._tmp_path / "videos"
        routes_module.VIDEOS_DIR.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self._sms_patch = mock.patch.object(sms_module, "ALIYUN_SMS_ACCESS_KEY_ID", "")
        self._sms_patch.start()
        self._client_cm = TestClient(main.app)
        self.client = self._client_cm.__enter__()
        # 清空登录失败记录，保证用例间互不影响
        conn = sqlite3.connect(str(auth_module.SYSTEM_DB_PATH))
        try:
            conn.execute("DELETE FROM login_fails")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self._client_cm.__exit__(None, None, None)
        self._sms_patch.stop()

    def _register(self, phone, password="pass1234"):
        _seed_sms(phone)
        r = self.client.post(
            "/api/register",
            json={"phone": phone, "sms_code": "123456", "password": password},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---------------- 登录锁定 ----------------

    def test_login_lock_after_5_failures(self):
        self._register("13811110001")
        for i in range(5):
            r = self.client.post("/api/login", json={"username": "13811110001", "password": "wrong"})
            self.assertEqual(r.status_code, 401, f"第{i + 1}次应 401")
        # 第 6 次（同一 IP testclient + 账号）应被锁定 423
        r = self.client.post("/api/login", json={"username": "13811110001", "password": "pass1234"})
        self.assertEqual(r.status_code, 423)
        self.assertTrue(r.headers.get("Retry-After"))
        self.assertIn("锁定", r.json()["detail"])

    def test_login_success_clears_lock(self):
        self._register("13811110002")
        # 4 次失败（未达阈值）
        for _ in range(4):
            self.client.post("/api/login", json={"username": "13811110002", "password": "wrong"})
        # 正确密码登录成功（不触发锁定）
        r = self.client.post("/api/login", json={"username": "13811110002", "password": "pass1234"})
        self.assertEqual(r.status_code, 200)
        # 清除锁定后错误密码不再 423（fail_count 已清零）
        r = self.client.post("/api/login", json={"username": "13811110002", "password": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_lock_progressive_duration(self):
        """函数级验证递进档位：5/10/20 次 → 300/1800/7200 秒。"""
        key = "prog-user"
        self.assertEqual(auth_module._lock_duration(4), 0)
        for n in range(1, 5):
            auth_module._record_login_fail("user", key)
        self.assertEqual(auth_module._lock_remaining("user", key), 0)      # 未达阈值不锁
        auth_module._record_login_fail("user", key)                        # 第 5 次 → 300s 档
        self.assertGreater(auth_module._lock_remaining("user", key), 0)
        self.assertLessEqual(auth_module._lock_remaining("user", key), 301)
        for _ in range(5):
            auth_module._record_login_fail("user", key)                    # 累计到 10 → 1800s 档
        self.assertGreaterEqual(auth_module._lock_remaining("user", key), 1800 - 60)
        auth_module._clear_login_fail("user", key)
        self.assertEqual(auth_module._lock_remaining("user", key), 0)

    # ---------------- 封禁 ----------------

    def _ban_user(self, uid):
        # 切换 admin 身份执行封禁（普通用户无权限）
        r = self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/admin/users/{}/ban".format(uid))
        self.assertEqual(r.status_code, 200, r.text)

    def test_ban_blocks_login_and_requests(self):
        """封禁语义：登录 403；解封恢复。"""
        user = self._register("13811110004")
        # admin 封禁
        self._ban_user(user["uid"])
        # 被封禁用户登录 → 403（即使密码正确）
        r = self.client.post("/api/login", json={"username": "13811110004", "password": "pass1234"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("封禁", r.json()["detail"])
        # admin 解封
        self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        r = self.client.post("/api/admin/users/{}/unban".format(user["uid"]))
        self.assertEqual(r.status_code, 200)
        # 解封后用户可重新登录
        r = self.client.post("/api/login", json={"username": "13811110004", "password": "pass1234"})
        self.assertEqual(r.status_code, 200)

    def test_ban_revokes_active_session(self):
        """已登录用户被封禁后，其现有会话请求业务接口 → 403。"""
        user = self._register("13811110006")
        # 直接改库模拟：用户持有有效 Cookie 时被 admin 封禁
        conn = sqlite3.connect(str(auth_module.SYSTEM_DB_PATH))
        try:
            conn.execute("UPDATE users SET status='banned', banned_at=? WHERE uid=?", ("now", user["uid"]))
            conn.commit()
        finally:
            conn.close()
        # 现有会话立即失效（get_current_user 拦截）
        r = self.client.get("/api/me")
        self.assertEqual(r.status_code, 403)
        self.assertIn("封禁", r.json()["detail"])

    def test_ban_requires_admin(self):
        user = self._register("13811110005")
        # 普通 user 调 ban → 403
        r = self.client.post("/api/admin/users/{}/ban".format(user["uid"]))
        self.assertEqual(r.status_code, 403)

    def test_ban_protects_admin_accounts(self):
        # admin 不能封禁 dev
        r = self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/admin/users/dev/ban")
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能封禁", r.json()["detail"])

    # ---------------- 改密 ----------------

    def test_change_password(self):
        user = self._register("13811110014", password="oldpass12")
        # 旧密码错
        r = self.client.post("/api/password/change", json={"old_password": "wrongpass", "new_password": "newpass12"})
        self.assertEqual(r.status_code, 401)
        # 正常改密
        r = self.client.post("/api/password/change", json={"old_password": "oldpass12", "new_password": "newpass12"})
        self.assertEqual(r.status_code, 200)
        # 改后旧密码无法登录
        r = self.client.post("/api/login", json={"username": "13811110014", "password": "oldpass12"})
        self.assertEqual(r.status_code, 401)
        # 新密码可登录
        r = self.client.post("/api/login", json={"username": "13811110014", "password": "newpass12"})
        self.assertEqual(r.status_code, 200)

    # ---------------- 短信重置密码 ----------------

    def test_password_reset_via_sms(self):
        user = self._register("13811110007", password="origpass1")
        # 未发短信直接重置 → 400（无发送记录）
        r = self.client.post("/api/password/reset", json={"phone": "13811110007", "sms_code": "123456", "new_password": "resetpass1"})
        self.assertEqual(r.status_code, 400)
        # 发 reset 类型短信
        _seed_sms("13811110007", "reset")
        r = self.client.post("/api/password/reset", json={"phone": "13811110007", "sms_code": "123456", "new_password": "resetpass1"})
        self.assertEqual(r.status_code, 200, r.text)
        # 重置后自动登录（Cookie）+ 新密码可登录，旧密码失效
        self.assertEqual(self.client.get("/api/me").json()["uid"], user["uid"])
        self.client.post("/api/logout")
        r = self.client.post("/api/login", json={"username": "13811110007", "password": "origpass1"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/login", json={"username": "13811110007", "password": "resetpass1"})
        self.assertEqual(r.status_code, 200)

    def test_password_reset_unknown_phone_404(self):
        _seed_sms("13811119999", "reset")
        r = self.client.post("/api/password/reset", json={"phone": "13811119999", "sms_code": "123456", "new_password": "resetpass1"})
        self.assertEqual(r.status_code, 404)

    # ---------------- 换绑 ----------------

    def test_phone_rebind(self):
        user = self._register("13811110008")
        _seed_sms("13811110008", "rebind")      # 旧手机码
        _seed_sms("13811110009", "rebind")      # 新手机码
        r = self.client.post("/api/phone/rebind", json={
            "old_sms_code": "123456", "new_phone": "13811110009", "new_sms_code": "123456",
        })
        self.assertEqual(r.status_code, 200, r.text)
        # 新手机可登录、旧手机失效
        r = self.client.post("/api/login", json={"username": "13811110009", "password": "pass1234"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/login", json={"username": "13811110008", "password": "pass1234"})
        self.assertEqual(r.status_code, 401)

    def test_phone_rebind_conflict_409(self):
        self._register("13811110010")
        self._register("13811110011")
        # 重新以第一个用户身份登录（第二个注册已切走会话）
        r = self.client.post("/api/login", json={"username": "13811110010", "password": "pass1234"})
        self.assertEqual(r.status_code, 200)
        # 旧手机码 + 占用新号
        _seed_sms("13811110010", "rebind")
        r = self.client.post("/api/phone/rebind", json={
            "old_sms_code": "123456", "new_phone": "13811110011", "new_sms_code": "123456",
        })
        self.assertEqual(r.status_code, 409)
        self.assertIn("已被其他账号", r.json()["detail"])

    # ---------------- 昵称 ----------------

    def test_nickname_update(self):
        user = self._register("13811110012")
        # 默认昵称
        me = self.client.get("/api/me").json()
        self.assertEqual(me["nickname"], "用户0012")
        # 修改昵称
        r = self.client.post("/api/profile/nickname", json={"nickname": "小屿"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/me").json()["nickname"], "小屿")
        # 空昵称/超长
        r = self.client.post("/api/profile/nickname", json={"nickname": ""})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/profile/nickname", json={"nickname": "x" * 17})
        self.assertEqual(r.status_code, 400)

    # ---------------- 迁移断言 ----------------

    def test_users_table_has_status_and_nickname(self):
        conn = sqlite3.connect(str(auth_module.SYSTEM_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        finally:
            conn.close()
        self.assertIn("status", cols)
        self.assertIn("nickname", cols)
        self.assertIn("banned_at", cols)


if __name__ == "__main__":
    unittest.main()