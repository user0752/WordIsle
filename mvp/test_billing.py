"""手机号注册 + 图形验证码/短信验证码 + 余额计费 + 游客升级 回归测试。

覆盖：
  1. 注册：手机号+短信码+密码 → 创建 user 账号、赠 50 币、自动登录；重复手机号 409
  2. 验证码：缺图形码 401、图形码错 400、60s 冷却 429
  3. 计费：注册用户生成扣费（流水 balance_after 正确）；余额不足 402；dev/admin 不扣费；
     guest 仍按每日配额
  4. 赠送：注册 +50、游客升级再 +50
  5. 卡密：admin 生成 → 用户兑换 → 加余额；重复兑换 400、伪造卡密 404
  6. 升级：游客数据迁移到新 uid 库、旧库文件删除、Cookie 指向新 uid；非 guest 403
  7. 迁移：users 表含 phone/balance 列

运行：cd mvp && python -m unittest test_billing -v
"""
import hashlib
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
import sms as sms_module
import verification as verification_module
from verification import TOLERANCE


def _seed_sms(phone, expected_type="register"):
    import time
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


def register_flow(client, phone, password="pass1234"):
    """测试模式（未配置阿里云短信）注册：先落库固定码，再用 123456 注册。返回响应。"""
    _seed_sms(phone)
    return client.post(
        "/api/register",
        json={"phone": phone, "sms_code": "123456", "password": password},
    )


class BillingTestCase(unittest.TestCase):
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
        # 联合跑时前面模块已把 uid 加入 _initialized_dbs 缓存，换目录后会跳过建库；
        # 清空缓存确保本模块在建新临时目录下完整初始化
        db_module._initialized_dbs.clear()
        main.AUDIOS_DIR = db_module.AUDIOS_DIR = routes_module.AUDIOS_DIR = cls._tmp_path / "audios"
        db_module.AUDIOS_DIR.mkdir(exist_ok=True)
        main.VIDEOS_DIR = routes_module.VIDEOS_DIR = cls._tmp_path / "videos"
        routes_module.VIDEOS_DIR.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        # 确保测试模式：清空阿里云短信密钥（sms_configured 返回 False → 固定验证码 123456）
        self._sms_patch = mock.patch.object(sms_module, "ALIYUN_SMS_ACCESS_KEY_ID", "")
        self._sms_patch.start()
        self._client_cm = TestClient(main.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self):
        self._client_cm.__exit__(None, None, None)
        self._sms_patch.stop()

    def _get_captcha(self):
        """获取 (captcha_id, 正确 captcha_x)。滑块验证码：target 坐标字符串（测试专用）。"""
        target = 150                       # 确定值便于断言
        cid = verification_module.new_captcha_id(str(target))
        return cid, str(target)

    # ---------------- 注册 ----------------

    def test_register_success_creates_user_with_balance(self):
        r = register_flow(self.client, "13800000001")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["role"], "user")
        self.assertTrue(data["uid"].startswith("u-"))
        self.assertEqual(data["balance"], 50)
        # 自动登录
        me = self.client.get("/api/me").json()
        self.assertEqual(me["role"], "user")
        self.assertEqual(me["balance"], 50)
        # 流水含注册赠送
        billing = self.client.get("/api/billing").json()
        self.assertEqual(billing["balance"], 50)
        self.assertEqual(billing["transactions"][0]["type"], "register_gift")
        self.assertEqual(billing["transactions"][0]["amount"], 50)

    def test_register_duplicate_phone_409(self):
        register_flow(self.client, "13800000002")
        # 重新落库一个新码（旧码已 used），短信校验通过后撞手机号 → 409
        _seed_sms("13800000002")
        r = self.client.post(
            "/api/register",
            json={"phone": "13800000002", "sms_code": "123456", "password": "pass1234"},
        )
        self.assertEqual(r.status_code, 409)

    def test_register_bad_sms_code_400(self):
        # 先落库固定码，再用错误码注册 → 400
        _seed_sms("13800000003")
        r = self.client.post(
            "/api/register",
            json={"phone": "13800000003", "sms_code": "000000", "password": "pass1234"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("验证码", r.json()["detail"])

    # ---------------- 验证码 ----------------

    def test_sms_send_requires_captcha(self):
        r = self.client.post("/api/sms/send", json={"phone": "13800000004"})
        self.assertEqual(r.status_code, 401)

    def test_sms_send_wrong_captcha_400(self):
        # captcha_id 不存在 / captcha_x 错误坐标均 400
        r = self.client.post(
            "/api/sms/send",
            json={"phone": "13800000004", "captcha_id": "nope", "captcha_x": "150"},
        )
        self.assertEqual(r.status_code, 400)
        cid, _ = self._get_captcha()
        r2 = self.client.post(
            "/api/sms/send",
            json={"phone": "13800000004", "captcha_id": cid, "captcha_x": "10"},
        )
        self.assertEqual(r2.status_code, 400)

    def test_sms_send_cooldown_429(self):
        cid, captcha_x = self._get_captcha()
        body = {"phone": "13800000005", "captcha_id": cid, "captcha_x": captcha_x}
        self.assertEqual(self.client.post("/api/sms/send", json=body).status_code, 200)
        cid2, captcha_x2 = self._get_captcha()
        r2 = self.client.post(
            "/api/sms/send", json={"phone": "13800000005", "captcha_id": cid2, "captcha_x": captcha_x2}
        )
        self.assertEqual(r2.status_code, 429)

    def test_sms_send_tolerance_ok(self):
        """容差内坐标（target±TOLERANCE）应通过，验证滑块容差语义（函数级，避免短信冷却）。"""
        target = 150
        for dx in (-TOLERANCE, 0, TOLERANCE):
            cid = verification_module.new_captcha_id(str(target))
            self.assertTrue(verification_module.verify_captcha(cid, str(target + dx)), f"容差 {dx} 应通过")
        cid_out = verification_module.new_captcha_id(str(target))
        self.assertFalse(verification_module.verify_captcha(cid_out, str(target + TOLERANCE + 1)), "超容差应拒绝")

    # ---------------- 计费 ----------------

    def _enrich_single(self, word="billword"):
        """调用单点流式端点（enrich bucket，单价 1 币）。mock TTS 避免外呼。"""
        with mock.patch.object(routes_module, "_ensure_word_audio", new=mock.AsyncMock(return_value="")):
            return self.client.post(
                "/api/words/single-stream",
                json={"word": word, "pos": "n.", "meaning_zh": "x", "frequency_level": "★☆☆☆☆"},
            )

    def test_user_billing_deducts_balance(self):
        register_flow(self.client, "13800000010")
        for i in range(3):
            r = self._enrich_single(f"billword{i}")
            self.assertEqual(r.status_code, 200, f"第{i + 1}次应扣费成功")
        billing = self.client.get("/api/billing").json()
        self.assertEqual(billing["balance"], 50 - 3)  # 单价 1
        cons = [t for t in billing["transactions"] if t["type"] == "consume"]
        self.assertEqual(len(cons), 3)
        self.assertEqual(cons[0]["amount"], -1)          # 最近一笔为倒数第 3 次消费
        self.assertEqual(cons[0]["balance_after"], 47)

    def test_user_insufficient_balance_402(self):
        register_flow(self.client, "13800000011")
        # 把余额全部扣成 0，enrich 单价 1 → 不足
        uid = self.client.get("/api/me").json()["uid"]
        auth_module.credit(uid, -50, "admin_credit", ref="test-setup")
        r = self._enrich_single("poorword")
        self.assertEqual(r.status_code, 402, r.text)
        self.assertIn("余额不足", r.json()["detail"])

    def test_dev_and_admin_not_charged(self):
        # dev
        self.client.post("/api/login", json={"username": "dev", "password": "dev-pass"})
        for i in range(3):
            r = self._enrich_single(f"devword{i}")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/me").json()["balance"], 0)
        # admin 也不扣（无余额即不限）
        self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        r = self._enrich_single("adminword")
        self.assertEqual(r.status_code, 200)

    def test_guest_still_uses_daily_quota(self):
        self.client.post("/api/login-guest")
        with mock.patch.object(routes_module, "_ensure_word_audio", new=mock.AsyncMock(return_value="")):
            for i in range(5):
                r = self.client.post(
                    "/api/words/single-stream",
                    json={"word": f"gq{i}", "pos": "n.", "meaning_zh": "x", "frequency_level": "★☆☆☆☆"},
                )
                self.assertEqual(r.status_code, 200)
        r6 = self.client.post(
            "/api/words/single-stream",
            json={"word": "gq6", "pos": "n.", "meaning_zh": "x", "frequency_level": "★☆☆☆☆"},
        )
        self.assertEqual(r6.status_code, 429)

    # ---------------- 卡密 ----------------

    def test_redeem_code_flow(self):
        # admin 生成卡密
        self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        r = self.client.post(
            "/api/admin/codes/generate",
            json={"batch": "T", "amount": 100, "count": 1},
        )
        self.assertEqual(r.status_code, 200)
        code = r.json()["codes"][0]
        # 注册用户兑换
        register_flow(self.client, "13800000020")
        self.assertEqual(self.client.get("/api/me").json()["balance"], 50)
        r2 = self.client.post("/api/redeem", json={"code": code})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["balance"], 150)
        # 重复兑换
        self.assertEqual(self.client.post("/api/redeem", json={"code": code}).status_code, 400)
        # 伪造卡密
        self.assertEqual(self.client.post("/api/redeem", json={"code": "FAKE1234"}).status_code, 404)

    def test_admin_credit_manual(self):
        register_flow(self.client, "13800000021")
        uid = self.client.get("/api/me").json()["uid"]
        self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        r = self.client.post(
            "/api/admin/credit", json={"uid": uid, "amount": 200, "note": "测试充值"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["balance"], 250)

    def test_admin_users_list(self):
        register_flow(self.client, "13800000022")
        self.client.post("/api/login", json={"username": "admin1", "password": "admin1-pass"})
        r = self.client.get("/api/admin/users")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(u["role"] == "user" for u in r.json()["users"]))

    # ---------------- 游客升级 ----------------

    def test_guest_upgrade_migrates_data(self):
        # 游客先登录并写入一个词 + 一条 generations（uuid 主键表）
        self.client.post("/api/login-guest")
        guest_uid = self.client.get("/api/me").json()["uid"]
        r = self._enrich_single("upgradeword1")
        self.assertEqual(r.status_code, 200, r.text)
        conn = sqlite3.connect(str(db_module._user_db_path(guest_uid)))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO generations (id, words, panels) VALUES (?,?,?)",
                ("gen-uuid-0001", '["upgradeword1"]', "[]"),
            )
            conn.commit()
        finally:
            conn.close()
        # 先落库固定码（guest_upgrade 类型），再升级
        _seed_sms("13800000030", "guest_upgrade")
        r2 = self.client.post(
            "/api/guest/upgrade",
            json={"phone": "13800000030", "sms_code": "123456", "password": "pass1234"},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        data = r2.json()
        self.assertEqual(data["role"], "user")
        self.assertEqual(data["balance"], 50)  # 仅升级赠送（非注册赠送）
        new_uid = data["uid"]
        # 数据迁移到新库：words 与 generations（uuid 主键表）都应在
        conn = sqlite3.connect(str(db_module._user_db_path(new_uid)))
        try:
            n = conn.execute("SELECT COUNT(*) c FROM words WHERE word='upgradeword1'").fetchone()[0]
            self.assertEqual(n, 1)
            g = conn.execute("SELECT COUNT(*) c FROM generations").fetchone()[0]
            self.assertEqual(g, 1, "generations（uuid 主键）应从游客库迁移")
        finally:
            conn.close()
        # 升级后仍能正常新增词（自增 id 不与复制过来的 id 冲突）
        r = self._enrich_single("upgradeword2")
        self.assertEqual(r.status_code, 200, r.text)
        conn = sqlite3.connect(str(db_module._user_db_path(new_uid)))
        try:
            n2 = conn.execute("SELECT COUNT(*) c FROM words WHERE word='upgradeword2'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n2, 1)
        # 旧游客库文件已删除
        self.assertFalse(db_module._user_db_path(guest_uid).exists())
        # Cookie 指向新 uid
        me = self.client.get("/api/me").json()
        self.assertEqual(me["uid"], new_uid)
        self.assertEqual(me["role"], "user")
        # 升级流水已记录（升级赠礼 + 后续单点消费均在）
        billing = self.client.get("/api/billing").json()
        types = [t["type"] for t in billing["transactions"]]
        self.assertIn("upgrade_gift", types)
        gift = [t for t in billing["transactions"] if t["type"] == "upgrade_gift"][0]
        self.assertEqual(gift["amount"], 50)

    def test_upgrade_requires_guest(self):
        # dev 不能升级
        self.client.post("/api/login", json={"username": "dev", "password": "dev-pass"})
        r = self.client.post(
            "/api/guest/upgrade",
            json={"phone": "13800000031", "sms_code": "123456", "password": "pass1234"},
        )
        self.assertEqual(r.status_code, 403)

    # ---------------- 迁移断言 ----------------

    def test_users_table_has_phone_and_balance(self):
        conn = sqlite3.connect(str(auth_module.SYSTEM_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        finally:
            conn.close()
        self.assertIn("phone", cols)
        self.assertIn("phone_verified", cols)
        self.assertIn("balance", cols)


if __name__ == "__main__":
    unittest.main()