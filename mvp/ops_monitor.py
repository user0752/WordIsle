#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WordIsle 服务器运维巡检脚本

用法（需在 mvp 目录下用项目 venv 运行）:
    python ops_monitor.py --check     # 高频巡检：检测高危项并即时推送（每日去重）
    python ops_monitor.py --report    # 每日早报：全量指标 + LLM 总结推送
    python ops_monitor.py --dry-run   # 采集并打印早报内容，不推送（本地/联调用）
    python ops_monitor.py --test      # 发送一条测试推送，验证 Server酱 Key

依赖:
    - 服务器环境: venv 里的 requests、python-dotenv
    - 配置: mvp/.env 中的 SERVERCHAN_SENDKEY（推送用）；LLM 复用 IMAGE_API_KEY + BAILIAN_LLM_BASE_URL
    - 运行身份: deploy（cron 中设置 PATH 即可），服务/systemd/日志需可读

规则阈值可通过 OPS_* 环境变量覆盖（见 THRESHOLDS）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config  # noqa: F401  触发 dotenv 加载并读取常用常量（IMAGE_API_KEY/BAILIAN_LLM_BASE_URL/LLM_MODELS）

import requests

# ========================================================================
# 常量
# ========================================================================
BASE_DIR = Path(__file__).resolve().parent
APP_LOG = BASE_DIR / "logs" / "app.log"
STATE_FILE = BASE_DIR / "data" / "ops_state.json"
NGINX_ACCESS = Path("/var/log/nginx/access.log")
AUTH_LOG = Path("/var/log/auth.log")
SVC_NAME = "wordisle"
HEALTH_URL = "http://127.0.0.1:8000/"
PUSH_URL = "https://sctapi.ftqq.com/{}.send"
LOG_SCAN_LINES = 50000          # 只扫 app.log 最后 N 行，控制耗时
NGINX_SCAN_LINES = 20000        # 只扫 access.log 最后 N 行
SEND_KEY = os.getenv("SERVERCHAN_SENDKEY", "").strip()

# 危险路径扫描特征（命中即判定为扫描/攻击尝试）
SCAN_PATTERNS = (
    r"\.env", r"wp-login", r"phpmyadmin", r"pma/", r"\.git", r"actuator",
    r"phpinfo", r"\.bak", r"\.sql", r"\.json\.", r"xdebug", r"server-status",
    r"\.aws", r"\.ssh", r"shell", r"c99", r"cmd\.php", r"eval", r"wget ",
    r"/admin", r"bypass", r"proxy", r"ADLogin",
)
_SCAN_RE = re.compile("|".join(SCAN_PATTERNS), re.IGNORECASE)

THRESHOLDS = {
    "disk_warn": int(os.getenv("OPS_DISK_WARN", "80")),
    "disk_crit": int(os.getenv("OPS_DISK_CRIT", "90")),
    "mem_warn": int(os.getenv("OPS_MEM_WARN", "85")),
    "mem_crit": int(os.getenv("OPS_MEM_CRIT", "92")),
    "load_warn": float(os.getenv("OPS_LOAD_WARN", "4")),
    "load_crit": float(os.getenv("OPS_LOAD_CRIT", "8")),
    "restart_warn": int(os.getenv("OPS_RESTART_WARN", "3")),
    "llm_fail_warn": float(os.getenv("OPS_LLM_FAIL_WARN", "0.5")),
    "llm_err_warn": int(os.getenv("OPS_LLM_ERR_WARN", "20")),
    "ssh_warn": int(os.getenv("OPS_SSH_WARN", "20")),
    "ssh_crit": int(os.getenv("OPS_SSH_CRIT", "100")),
    "scan_warn": int(os.getenv("OPS_SCAN_WARN", "30")),
    "scan_crit": int(os.getenv("OPS_SCAN_CRIT", "200")),
    "http4_warn": int(os.getenv("OPS_HTTP4_WARN", "200")),
    "ip_hits_warn": int(os.getenv("OPS_IP_HITS_WARN", "500")),
}


# ========================================================================
# 工具函数
# ========================================================================
def run(cmd, timeout=60):
    """执行命令，返回 (returncode, 合并输出)。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        out = (p.stdout or "").strip()
        if p.stderr:
            out = f"{out}\n{p.stderr.strip()}" if out else p.stderr.strip()
        return p.returncode, out
    except Exception as e:  # noqa: BLE001
        return -1, f"执行失败: {e}"


def bin(name):
    return shutil.which(name) or name


def issue(level, iid, text):
    return {"level": level, "id": iid, "text": text}


def now42():
    """返回带时区的当前时间（与日志 ts 可比）。"""
    return datetime.now().astimezone()


def parse_log_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# ========================================================================
# 采集器
# ========================================================================
def collect_system(issues, out):
    th = THRESHOLDS
    # 磁盘
    rc, text = run([bin("df"), "-h", "/"])
    if rc == 0 and text:
        try:
            fields = text.splitlines()[-1].split()
            disk_use = int(fields[4].rstrip("%"))
            out.append(("磁盘 /", f"{disk_use}% (可用 {fields[3]})"))
            if disk_use >= th["disk_crit"]:
                issues.append(issue("critical", "disk_full", f"根分区已用 {disk_use}%，需清理或扩容"))
            elif disk_use >= th["disk_warn"]:
                issues.append(issue("warn", "disk_high", f"根分区已用 {disk_use}%"))
        except (ValueError, IndexError):
            out.append(("磁盘 /", "解析失败"))
    else:
        out.append(("磁盘 /", "N/A"))

    # 内存
    rc, text = run([bin("free"), "-m"])
    if rc == 0 and text and text.startswith("Mem:"):
        parts = text.split()
        total, avail = int(parts[1]), int(parts[6])
        mem_use = round((total - avail) / total * 100) if total else 0
        out.append(("内存", f"{mem_use}% (可用 {avail}MB/{total}MB)"))
        if mem_use >= th["mem_crit"]:
            issues.append(issue("critical", "mem_full", f"内存已用 {mem_use}%"))
        elif mem_use >= th["mem_warn"]:
            issues.append(issue("warn", "mem_high", f"内存已用 {mem_use}%"))
    else:
        out.append(("内存", "N/A"))

    # 负载与运行时长
    rc, text = run([bin("uptime")])
    if rc == 0 and text:
        up = re.search(r"up\s+(.+?),\s+\d+\s+users?", text)
        if up:
            out.append(("运行时长", up.group(1)))
        loads = text.rsplit("load average:", 1)
        if len(loads) == 2:
            l1, l5, l15 = (float(x) for x in re.findall(r"[\d.]+", loads[1])[:3])
            out.append(("负载 (1/5/15分)", f"{l1:.2f} / {l5:.2f} / {l15:.2f}"))
            if l1 >= th["load_crit"]:
                issues.append(issue("critical", "load_high", f"负载过高 1分钟均值 {l1:.2f}"))
            elif l1 >= th["load_warn"]:
                issues.append(issue("warn", "load_mid", f"负载偏高 1分钟均值 {l1:.2f}"))


def collect_service(issues, out):
    rc, text = run([bin("systemctl"), "is-active", SVC_NAME])
    active = rc == 0 and text.strip() == "active"
    out.append(("服务状态", text.strip() or "未知"))
    if not active:
        issues.append(issue("critical", "svc_down", f"systemd 服务 {SVC_NAME} 未在运行 (is-active: {text.strip() or 'rc=%d' % rc})"))

    rc, text = run([bin("systemctl"), "show", SVC_NAME, "-p", "NRestarts"])
    restarts = 0
    m = re.search(r"NRestarts=(\d+)", text)
    if m:
        restarts = int(m.group(1))
        out.append(("服务重启次数", str(restarts)))
        if restarts > THRESHOLDS["restart_warn"]:
            issues.append(issue("warn", "svc_restart", f"服务 24h 内重启 {restarts} 次，疑似不稳定"))

    try:
        r = requests.get(HEALTH_URL, timeout=8)
        out.append(("健康检查 (8000)", f"HTTP {r.status_code}"))
        if r.status_code >= 500:
            issues.append(issue("warn", "http_5xx", f"首页健康检查返回 {r.status_code}"))
    except Exception as e:  # noqa: BLE001
        out.append(("健康检查 (8000)", f"连接失败 ({type(e).__name__})"))
        issues.append(issue("critical", "http_down", "127.0.0.1:8000 无法访问"))


def collect_llm(issues, out):
    """从 app.log 统计近 24h 的模型调用成功/失败情况。"""
    th = THRESHOLDS
    if not APP_LOG.exists():
        out.append(("模型调用 (近24h)", "无日志文件"))
        return
    now = now42()
    since = now - timedelta(hours=24)
    start = ok = err = 0
    err_samples = []
    with open(APP_LOG, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "LLM 流式调用" not in line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            t = parse_log_ts(j.get("ts", ""))
            if t is not None and t < since:
                continue
            msg = j.get("msg", "")
            if "流式调用" in msg:
                start += 1
                if "成功" in msg:
                    ok += 1
            if j.get("level") == "ERROR" or any(k in msg for k in ("失败", "超时", "异常", "Exception", "error")):
                err += 1
                if len(err_samples) < 5:
                    err_samples.append(f"[{j.get('svc', '?')}] {msg[:100]}")
    fail_rate = (err / start) if start else (1 if err else 0)
    out.append(("模型调用 (近24h)", f"成功 {ok}/{start} · 失败 {err} 次"))
    if err:
        out.append(("模型调用异常样例", "；".join(err_samples)))
    if start and err and fail_rate >= 1.0:
        issues.append(issue("critical", "llm_all_fail", f"近24h 模型调用全部失败 ({err} 次)，服务降级"))
    elif start and fail_rate >= th["llm_fail_warn"]:
        issues.append(issue("warn", "llm_high_fail", f"近24h 模型调用失败率 {fail_rate:.0%} ({err}/{start})"))
    elif err >= th["llm_err_warn"] and start == 0:
        issues.append(issue("warn", "llm_many_err", f"近24h 检测到 {err} 次模型相关错误"))


def collect_security(issues, out):
    th = THRESHOLDS
    # SSH 失败登录：优先 /var/log/auth.log（本机 sshd 走 rsyslog，journald 几乎收不到）
    # 只取末尾若干行控制采集成本（覆盖最近数小时，足以判定爆破）
    ssh_fail = -1
    if AUTH_LOG.exists():
        rc, text = run([bin("tail"), "-n", "200000", str(AUTH_LOG)], timeout=120)
        if rc != 0:
            # deploy 不在 adm 组时用 sudo 免密读（服务器已配 NOPASSWD）
            rc, text = run(["sudo", "-n", bin("tail"), "-n", "200000", str(AUTH_LOG)], timeout=120)
        if rc == 0 and text:
            fails = re.findall(r"Failed password .*?from (\S+)", text)
            ssh_fail = len(fails)
            if fails:
                src_ips = sorted(set(fails))
                out.append(("SSH 失败来源 IP 数", str(len(src_ips))))
                out.append(("SSH 高频来源(%d)" % min(len(src_ips), 5), "、".join(src_ips[:5])))
    if ssh_fail < 0:
        # 降级：journald 路径（部分发行版）
        for unit in ("sshd", "ssh"):
            rc, text = run([bin("journalctl"), "-u", unit, "--since", "24 hours ago", "--no-pager"])
            if rc == 0 and text:
                ssh_fail = len(re.findall(r"Failed password", text))
                break
            if rc != 0 and "Permission denied" in text:
                break
    if ssh_fail >= 0:
        out.append(("SSH 失败登录 (近段)", f"{ssh_fail} 次"))
        if ssh_fail >= th["ssh_crit"]:
            issues.append(issue("critical", "ssh_brute", f"SSH 失败登录 {ssh_fail} 次，疑似暴力破解（fail2ban 已自动封禁高频 IP）"))
        elif ssh_fail >= th["ssh_warn"]:
            issues.append(issue("warn", "ssh_brute_low", f"SSH 失败登录 {ssh_fail} 次"))
    else:
        out.append(("SSH 失败登录", "无权限/无日志 (可将 deploy 加入 adm 组)"))

    # nginx 访问日志：扫描特征 / 401 / 4xx、5xx / 高频 IP
    if NGINX_ACCESS.exists():
        rc, text = run([bin("tail"), "-n", str(NGINX_SCAN_LINES), str(NGINX_ACCESS)])
        scan = total = c401 = c4 = c5 = 0
        ip_hits = {}
        for line in text.splitlines():
            m = re.match(r"^(\S+) - - \[([^\]]+)\] \"(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH) (\S+)[^\"]*\" (\d{3})", line)
            if not m:
                continue
            total += 1
            ip, path, code = m.group(1), m.group(4), int(m.group(5))
            ip_hits[ip] = ip_hits.get(ip, 0) + 1
            if 400 <= code < 500:
                c4 += 1
                if code == 401:
                    c401 += 1
            elif 500 <= code < 600:
                c5 += 1
            if _SCAN_RE.search(path):
                scan += 1
        out.append(("Nginx 访问 (近{:.0f}K行)".format(NGINX_SCAN_LINES / 1000), f"总 {total} · 4xx {c4} · 5xx {c5}"))
        out.append(("Nginx 401 (BasicAuth失败)", str(c401)))
        out.append(("Nginx 扫描/攻击特征", f"{scan} 次"))
        if scan >= th["scan_crit"]:
            issues.append(issue("critical", "scan_many", f"Nginx 检测到 {scan} 次攻击/扫描特征"))
        elif scan >= th["scan_warn"]:
            issues.append(issue("warn", "scan_some", f"Nginx 检测到 {scan} 次攻击/扫描特征"))
        if c401 >= th["http4_warn"]:
            issues.append(issue("warn", "auth_brute", f"Basic Auth 失败 {c401} 次，疑似爆破登录"))
        if ip_hits and 5 in ip_hits.values() and max(ip_hits.values()) >= th["ip_hits_warn"]:
            top_ip, top_hits = max(ip_hits.items(), key=lambda kv: kv[1])
            issues.append(issue("warn", "ip_hot", f"IP {top_ip} 高频访问 {top_hits} 次，疑似爬虫/攻击"))
        out.append(("Nginx 高频 IP", "、".join(f"{k}:{v}" for k, v in sorted(ip_hits.items(), key=lambda kv: -kv[1])[:3]) or "-"))
    else:
        out.append(("Nginx 日志", "无 /var/log/nginx/access.log"))

    # fail2ban 防护
    rc, text = run([bin("systemctl"), "is-active", "fail2ban"])
    f2b_active = rc == 0 and text.strip() == "active"
    out.append(("fail2ban 防爆破", "active" if f2b_active else "未安装/未启用"))
    if not f2b_active:
        issues.append(issue("info", "f2b_missing", "fail2ban 未启用，SSH/HTTP 爆破风险无自动封禁"))


def collect_cert(issues, out):
    certs = list(Path("/etc/letsencrypt/live").glob("*/cert.pem")) if Path("/etc/letsencrypt/live").exists() else []
    if not certs:
        out.append(("TLS 证书", "未启用 (http)"))
        return
    for cert in certs:
        rc, text = run([bin("openssl"), "x509", "-enddate", "-noout", "-in", str(cert)])
        m = re.search(r"notAfter=(.+)", text)
        if not m:
            continue
        try:
            end = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days = (end - datetime.now(timezone.utc)).days
            out.append((f"TLS 证书 {cert.parent.name}", f"剩余 {days} 天"))
            if days < 30:
                issues.append(issue("warn", "cert_expire", f"证书 {cert.parent.name} 仅剩 {days} 天"))
        except ValueError:
            out.append((f"TLS 证书 {cert.parent.name}", "解析失败"))


# ========================================================================
# 早报生成
# ========================================================================
def build_report():
    issues, out = [], []
    for fn in (collect_system, collect_service, collect_llm, collect_security, collect_cert):
        try:
            fn(issues, out)
        except Exception as e:  # noqa: BLE001
            issues.append(issue("warn", f"{fn.__name__}_fail", f"{fn.__name__} 采集异常: {e}"))

    crit = [i for i in issues if i["level"] == "critical"]
    warn = [i for i in issues if i["level"] == "warn"]
    status = "🔴" if crit else ("⚠️" if warn else "✅")
    status_word = "异常" if crit else ("关注" if warn else "正常")

    md = [
        f"## WordIsle 运维早报 · {now42():%m-%d %H:%M}",
        "",
        f"整体状态：**{status} {status_word}**（异常 {len(crit)} · 关注 {len(warn)}）",
        "",
        "### 分项指标",
    ]
    md += [f"- **{k}**：{v}" for k, v in out]
    if issues:
        md += ["", "### 问题清单"]
        for i in issues:
            tag = {"critical": "🔴", "warn": "⚠️", "info": "ℹ️"}[i["level"]]
            md.append(f"- {tag} {i['text']}")
    lm = llm_summary("\n".join(md))
    if lm:
        md += ["", "### AI 总结", lm]
    return "\n".join(md), status_word, issues, out


def llm_summary(report_md):
    """调用 LLM 生成通俗总结；失败返回空串（自动降级为纯规则报告）。"""
    key = os.getenv("OPS_LLM_API_KEY", "").strip() or getattr(config, "IMAGE_API_KEY", "")
    base = getattr(config, "BAILIAN_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model = os.getenv("OPS_LLM_MODEL", "").strip() or next(
        (m["model"] for m in config.LLM_MODELS if m.get("recommended")), "qwen-flash")
    if not key:
        return ""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是资深服务器运维专家。根据给定的巡检数据，用中文输出 200 字以内的总结：先给一句整体健康结论，再列出异常/风险点及具体处置建议。只输出总结正文，禁止 Markdown 标题与列表。"},
            {"role": "user", "content": report_md},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }
    headers = {"Authorization": f"Bearer {key}"}
    # 百炼系列支持关闭思考以降延迟；个别兼容端点不接受该参数，失败时去掉重试一次
    for first in (True, False):
        try:
            body = dict(payload)
            if first:
                body["enable_thinking"] = False
            r = requests.post(base.rstrip("/") + "/chat/completions", headers=headers, json=body, timeout=90)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


# ========================================================================
# 推送
# ========================================================================
def push(title, desp=""):
    if not SEND_KEY:
        return False, "未配置 SERVERCHAN_SENDKEY（.env 中设置）"
    try:
        r = requests.post(PUSH_URL.format(SEND_KEY), data={"title": title, "desp": desp}, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败: {e}"
    try:
        j = r.json()
        return j.get("code") == 0, j.get("message") or r.text[:200]
    except Exception:
        return False, r.text[:200]


# ========================================================================
# 状态存储（告警去重）
# ========================================================================
def _state_path():
    p = STATE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p
    except PermissionError:
        return Path("/tmp/wordisle_ops_state.json")


def load_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8")) if _state_path().exists() else {}
    except Exception:
        return {}


def save_state(st):
    try:
        _state_path().write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[ops] 状态写入失败: {e}", file=sys.stderr)


# ========================================================================
# 子命令
# ========================================================================
def run_check():
    """异常即时巡检（cron 每 15 分钟）：高危项每日去重推送。"""
    _, _, issues, _ = build_report()
    today = now42().strftime("%Y-%m-%d")
    crit = [i for i in issues if i["level"] == "critical"]
    st = load_state()
    alerts = st.setdefault("alerts", {})
    seen = alerts.get(today, {})
    new = [i for i in crit if i["id"] not in seen]
    if not new:
        print(f"[ops] {now42():%H:%M} 巡检完成，无新告警")
        return
    title = f"【WordIsle 告警】{len(new)} 项异常"
    desp = "\n".join(f"- 🔴 {i['text']}" for i in new)
    ok, msg = push(title, desp)
    if ok:
        for i in new:
            seen[i["id"]] = now42().strftime("%H:%M")
        alerts[today] = seen
        save_state(st)
        print(f"[ops] 已推送告警: {[i['id'] for i in new]}")
    else:
        print(f"[ops] 告警推送失败: {msg}", file=sys.stderr)


def run_report():
    md, status_word, _, _ = build_report()
    print(md)
    # 摘要行放入标题（免费版微信卡片只显示标题）
    brief = re.search(r"^## WordIsle 运维早报 · (\S+)", md)
    title = f"【WordIsle 早报 · {brief.group(1) if brief else now42():%m-%d}】{status_word}"
    ok, msg = push(title, md)
    print(f"\n[ops] 推送结果: {'成功' if ok else '失败: ' + msg}")


def run_dry():
    md, status_word, _, _ = build_report()
    print(md)


def run_test():
    ok, msg = push("【WordIsle】运维巡检助手测试", f"推送通道正常 ✓\n\n- 时间：{now42():%Y-%m-%d %H:%M}\n- Key：{SEND_KEY[:6]}…")
    print(f"[ops] 测试推送: {'成功' if ok else '失败: ' + msg}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="WordIsle 服务器运维巡检")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="高危项巡检+即时推送")
    g.add_argument("--report", action="store_true", help="每日早报推送")
    g.add_argument("--dry-run", action="store_true", help="采集并打印早报，不推送")
    g.add_argument("--test", action="store_true", help="发送测试推送")
    args = ap.parse_args()

    if args.test:
        sys.exit(run_test())
    if args.check:
        run_check()
    elif args.report:
        run_report()
    else:
        run_dry()


if __name__ == "__main__":
    main()