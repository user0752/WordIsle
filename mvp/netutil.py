"""网络工具：从请求中解析可信客户端 IP。

部署在 nginx 反向代理之后：
- nginx 通过 `proxy_set_header X-Real-IP $remote_addr` 覆盖该头，客户端无法伪造；
- `X-Forwarded-For` 由 `$proxy_add_x_forwarded_for` 追加，客户端自带的值在最左侧，
  只有最后一跳才是 nginx 看到的真实地址。

因此顺序为：X-Real-IP → X-Forwarded-For 最后一跳 → 直连地址。
禁止取 XFF 第一个值（那是客户端可控的）。
"""

LOCAL_IPS = {"127.0.0.1", "::1", "localhost", "testclient"}


def client_ip(request) -> str:
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "-"


def is_local_ip(ip: str) -> bool:
    """本机 / 测试客户端：用于放行短信测试模式与豁免限流。"""
    return (ip or "") in LOCAL_IPS
