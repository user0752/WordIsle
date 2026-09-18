"""阿里云短信（Dysmsapi）发送 + 测试模式降级。

- 配置齐全（KEY/SIGN/TEMPLATE）：httpx 直连 dysmsapi.aliyuncs.com，RPC 风格，
  HMAC-SHA1 签名（无需额外 SDK，复用项目已有 httpx 依赖）。
- 任一配置缺失：自动降级「测试模式」——验证码固定 123456，仅写日志不发真实短信，
  便于本地开发与回归测试。
"""
import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

from config import (
    ALIYUN_SMS_ACCESS_KEY_ID,
    ALIYUN_SMS_ACCESS_KEY_SECRET,
    ALIYUN_SMS_SIGN_NAME,
    ALIYUN_SMS_TEMPLATE_CODE,
)

logger = logging.getLogger("wordisle.sms")

_SMS_ENDPOINT = "https://dysmsapi.aliyuncs.com/"
_SMS_API_VERSION = "2017-05-25"
_SMS_ACTION = "SendSms"

# 测试模式固定验证码（未配置阿里云短信时使用，前端/测试均感知此常量）
TEST_MODE_CODE = "123456"


def sms_configured() -> bool:
    """是否配置了完整的阿里云短信参数。"""
    return bool(
        ALIYUN_SMS_ACCESS_KEY_ID
        and ALIYUN_SMS_ACCESS_KEY_SECRET
        and ALIYUN_SMS_SIGN_NAME
        and ALIYUN_SMS_TEMPLATE_CODE
    )


def _percent_encode(s: str) -> str:
    """RFC3986 百分号编码（阿里云 RPC 签名要求）。"""
    return urllib.parse.quote(s, safe="-_.~")


def _signature(query_params: dict, access_key_secret: str) -> str:
    """按阿里云 RPC 规范计算 HMAC-SHA1 签名。"""
    canonical = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}"
        for k, v in sorted(query_params.items())
    )
    string_to_sign = f"GET&%2F&{_percent_encode(canonical)}"
    key = f"{access_key_secret}&".encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    import base64
    return base64.b64encode(digest).decode()


def _send_real_sms(phone: str, code: str) -> bool:
    """调用阿里云 SendSms 真实发送。成功返回 True，任何异常返回 False。"""
    params = {
        "AccessKeyId": ALIYUN_SMS_ACCESS_KEY_ID,
        "Action": _SMS_ACTION,
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignName": ALIYUN_SMS_SIGN_NAME,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": f"{time.time():.6f}-{phone}",
        "SignatureVersion": "1.0",
        "TemplateCode": ALIYUN_SMS_TEMPLATE_CODE,
        "TemplateParam": f'{{"code":"{code}"}}',
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": _SMS_API_VERSION,
    }
    params["Signature"] = _signature(params, ALIYUN_SMS_ACCESS_KEY_SECRET)
    try:
        resp = httpx.get(_SMS_ENDPOINT, params=params, timeout=10)
        data = resp.json()
        if data.get("Code") == "OK":
            logger.info("短信发送成功 phone=%s code 已发送", phone)
            return True
        logger.warning("短信发送失败 phone=%s code=%s 响应=%s", phone, data.get("Code"), data)
        return False
    except Exception as e:
        logger.warning("短信发送异常 phone=%s err=%s", phone, e)
        return False


def send_sms_code(phone: str, code: str) -> bool:
    """发送短信验证码。配置齐全走真实发送；否则测试模式（固定码 + 日志）。
    返回是否进入真实发送流程（测试模式返回 False，调用方据此决定是否放行校验）。"""
    if sms_configured():
        return _send_real_sms(phone, code)
    logger.info("短信测试模式 phone=%s code=%s（未配置阿里云短信，使用固定验证码）", phone, TEST_MODE_CODE)
    return False