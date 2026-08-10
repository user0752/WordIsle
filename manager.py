"""
TOEIC 启动管理器
================
- 启动 / 停止 / 重启 TOEIC MVP 服务
- 实时捕获 stdout/stderr，按级别 (INFO/WARNING/ERROR) 高亮
- 周期性健康检查，监控前端 + 后端 API 状态
- 日志搜索、过滤、清空、保存到文件

启动:
  终端模式 (默认) : python manager.py --cli   （或双击 manager.bat）
  GUI 模式        : python manager.py          （或运行 manager.bat gui）
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import httpx
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ========================================================================
# 路径 & 配置
# ========================================================================

ROOT     = Path(__file__).resolve().parent
MVP_DIR  = ROOT / "mvp"
MAIN_PY  = MVP_DIR / "main.py"
ENV_FILE = MVP_DIR / ".env"
ENV_EX   = MVP_DIR / ".env.example"
LOG_DIR  = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

HOST = "localhost"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"

MAX_LOG_LINES = 5000     # 内存中保留的最大日志行数
TRIM_TO       = 3000     # 超过后裁剪到该数量

# ========================================================================
# 主题色（与 MVP 前端保持一致）
# ========================================================================

C = {
    "bg":      "#0f1117",
    "card":    "#1a1d27",
    "border":  "#2d3140",
    "text":    "#d4d6dd",
    "muted":   "#8b90a0",
    "accent":  "#6ea8fe",
    "green":   "#5fc596",
    "yellow":  "#f4a261",
    "red":     "#e24b4a",
    "log_time":"#5a5e6e",
}

FONT_UI   = ("Microsoft YaHei UI", 10)
FONT_MONO = ("Consolas", 10)

# ========================================================================
# 工具函数
# ========================================================================

_LEVEL_PATTERNS = [
    (re.compile(r"\bERROR\b|Traceback|Exception|ImportError|ModuleNotFoundError", re.I), "ERROR"),
    (re.compile(r"\bWARNING\b|\bWARN\b", re.I), "WARNING"),
    (re.compile(r"\bINFO\b",  re.I), "INFO"),
    (re.compile(r"\bDEBUG\b", re.I), "DEBUG"),
]

def detect_level(line: str) -> str:
    for pat, lvl in _LEVEL_PATTERNS:
        if pat.search(line):
            return lvl
    return "INFO"


def _test_python(cmd_list):
    """测试该 Python 是否能导入服务依赖。"""
    try:
        r = subprocess.run(
            cmd_list + ["-c", "import fastapi,uvicorn,httpx,dotenv,dashscope"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _resolve_python_path(cmd_list) -> str | None:
    """获取命令对应的 python.exe 真实路径。"""
    try:
        r = subprocess.run(
            cmd_list + ["-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def find_python() -> str:
    """查找已安装服务依赖 (fastapi/uvicorn/httpx/dotenv/dashscope) 的 Python。

    优先级:
      1. 项目专属 venv: mvp/venv/Scripts/python.exe (推荐，已配齐依赖)
      2. 环境变量 TOEIC_SERVICE_PYTHON (由 manager.bat 解析传入)
      3. 当前 Python (管理器自己用的)
      4. py launcher / PATH 中的 python (兜底探测)
    """
    # 1. 项目 venv（首选）
    venv_py = MVP_DIR / ("venv/Scripts/python.exe" if sys.platform == "win32"
                         else "venv/bin/python")
    if venv_py.exists() and _test_python([str(venv_py)]):
        return str(venv_py)

    # 2. .bat 解析的服务 Python 路径
    env_py = os.environ.get("TOEIC_SERVICE_PYTHON", "").strip()
    if env_py and Path(env_py).exists() and _test_python([env_py]):
        return env_py

    candidates: list[list[str]] = []

    # 3. 当前 Python（管理器自己用的）
    candidates.append([sys.executable])

    # 4. py launcher 各版本（Windows）
    if sys.platform == "win32":
        candidates.append(["py", "-3.13"])
        candidates.append(["py", "-3"])
        candidates.append(["py"])

    # 5. PATH 中的 python
    candidates.append(["python"])

    for cmd in candidates:
        if _test_python(cmd):
            resolved = _resolve_python_path(cmd)
            return resolved or cmd[0]

    # 都没装依赖时回退到当前 Python（启动时会报错并显示在日志里）
    return sys.executable


def check_url(url: str, timeout: float = 1.5) -> bool:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(url)
            return r.status_code == 200
    except Exception:
        return False


def check_health():
    try:
        with httpx.Client(timeout=1.5) as c:
            r = c.get(f"{BASE_URL}/api/health")
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def read_env_keys():
    """读取 .env 中的关键密钥是否已设置（不泄露值）。"""
    values = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    values[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return [
        ("DeepSeek", bool(values.get("DEEPSEEK_API_KEY"))),
        ("TTS", bool(values.get("TTS_API_KEY"))),
        ("文生图", bool(values.get("IMAGE_API_KEY") or values.get("TTS_API_KEY"))),
    ]


def build_self_check_report(svc) -> list[tuple[str, str]]:
    """执行一次性自检，返回 [(level, message), ...] 报告（GUI / CLI 共用）。"""
    report: list[tuple[str, str]] = []

    def add(level, msg):
        report.append((level, msg))

    add("INFO", f"Python 解释器: {svc.python_exe}")
    try:
        r = subprocess.run(
            [svc.python_exe, "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True, text=True, timeout=5,
        )
        add("INFO", f"Python 版本: {r.stdout.strip() or '未知'}")
    except Exception as e:
        add("WARNING", f"Python 版本读取失败: {e}")

    code = ("import importlib.util; "
            "mods=['fastapi','uvicorn','httpx','dotenv','dashscope']; "
            "print(','.join(m for m in mods if importlib.util.find_spec(m) is None))")
    try:
        r = subprocess.run([svc.python_exe, "-c", code],
                           capture_output=True, text=True, timeout=5)
        missing = [m for m in r.stdout.strip().split(",") if m]
        if missing:
            add("ERROR", f"缺少依赖模块: {', '.join(missing)}")
        else:
            add("INFO", "依赖模块: fastapi / uvicorn / httpx / dotenv / dashscope 全部可用")
    except Exception as e:
        add("ERROR", f"依赖检测失败: {e}")

    if MAIN_PY.exists():
        add("INFO", f"主程序 main.py: 存在 ({MAIN_PY.name})")
    else:
        add("ERROR", f"主程序 main.py: 不存在 ({MAIN_PY})")

    if ENV_FILE.exists():
        add("INFO", f".env 配置文件: 存在 ({ENV_FILE.name})")
        for label, ok in read_env_keys():
            add("INFO" if ok else "WARNING", f"{label} 密钥: {'已配置' if ok else '未配置'}")
    else:
        add("WARNING", f".env 配置文件: 不存在（可点击「编辑 .env」从示例复制）")

    db_path = MVP_DIR / "data" / "words.db"
    if db_path.exists():
        add("INFO", f"数据库 words.db: 存在 ({db_path.name})")
    else:
        add("WARNING", "数据库 words.db: 不存在（首次启动服务时自动创建）")

    if svc.is_running:
        add("INFO", f"服务进程: 运行中 (PID={svc.pid})")
    else:
        add("WARNING", "服务进程: 未运行（请点击「启动」）")

    fe_ok = check_url(BASE_URL, timeout=1.0)
    add("INFO" if fe_ok else "ERROR",
        f"前端 {BASE_URL}: {'可访问' if fe_ok else '不可访问'}")

    health = check_health() if fe_ok else None
    if health:
        add("INFO", "后端 /api/health: 正常")
        add("INFO" if health.get("db") else "WARNING",
            f"数据库: {'正常' if health.get('db') else '缺失'}")
        add("INFO" if health.get("deepseek_key") else "WARNING",
            f"DeepSeek: {'已配置' if health.get('deepseek_key') else '未配置'}")
        add("INFO" if health.get("tts_key") else "WARNING",
            f"TTS: {'已配置' if health.get('tts_key') else '未配置'}")
        add("INFO" if health.get("image_key") else "WARNING",
            f"文生图: {'已配置' if health.get('image_key') else '未配置'}")
        usage = health.get("daily_usage", {})
        if usage:
            add("INFO",
                f"今日用量: AI {usage.get('ai', 0)}/{usage.get('ai_limit', 0)} · "
                f"TTS {usage.get('tts', 0)}/{usage.get('tts_limit', 0)} · "
                f"图片 {usage.get('image', 0)}/{usage.get('image_limit', 0)}")
    else:
        add("ERROR", "后端 /api/health: 不可访问（服务未启动或端口异常）")

    return report


# ========================================================================
# 服务管理
# ========================================================================

class ServiceManager:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self.reader: threading.Thread | None = None
        self.start_time: float | None = None
        self.python_exe = find_python()
        self._stopping = False

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self):
        return self.process.pid if self.is_running else None

    @property
    def uptime(self) -> str:
        if not self.start_time or not self.is_running:
            return "00:00:00"
        d = int(time.time() - self.start_time)
        h, rem = divmod(d, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def start(self):
        if self.is_running:
            return False, "服务已在运行"
        if not MAIN_PY.exists():
            return False, f"找不到主程序: {MAIN_PY}"
        self._stopping = False
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            self.process = subprocess.Popen(
                [self.python_exe, "-u", str(MAIN_PY)],
                cwd=str(MVP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
                env=self._build_env(),
            )
        except Exception as e:
            return False, f"启动失败: {e}"
        self.start_time = time.time()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        return True, f"服务启动中... PID={self.process.pid}"

    def _build_env(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _read_loop(self):
        try:
            for line in self.process.stdout:
                if self._stopping:
                    break
                line = line.rstrip("\r\n")
                if not line:
                    continue
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_queue.put((ts, detect_level(line), line))
        except Exception as e:
            self.log_queue.put((datetime.now().strftime("%H:%M:%S"),
                                "ERROR", f"日志读取异常: {e}"))
        finally:
            code = self.process.poll() if self.process else None
            self.log_queue.put((datetime.now().strftime("%H:%M:%S"),
                                "INFO", f"--- 进程退出 (code={code}) ---"))

    def stop(self):
        if not self.is_running:
            return False, "服务未运行"
        self._stopping = True
        pid = self.process.pid
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            else:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception as e:
            return False, f"停止失败: {e}"
        try:
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None
        self.start_time = None
        return True, "服务已停止"

    def restart(self):
        if self.is_running:
            self.stop()
            time.sleep(0.5)
        return self.start()


# ========================================================================
# UI
# ========================================================================

class ManagerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("TOEIC 启动管理器")
        self.root.geometry("1040x760")
        self.root.minsize(940, 620)
        self.root.configure(bg=C["bg"])

        self.svc = ServiceManager()
        self.log_lines: list[tuple[str, str, str]] = []
        self.filter_level = "ALL"
        self.search_text = ""
        self.autoscroll = True
        self.health_data = None
        self.fe_ok = False
        self.be_ok = False

        # 增量日志渲染: 已渲染到第几行
        self._rendered_count = 0
        self._full_rebuild = True   # 首次完整重绘

        # 手动自检进行中标记
        self._self_checking = False

        self._setup_style()
        self._build_ui()
        self._poll_log_queue()
        # 前后端状态常驻监测（自检按钮只做日志诊断，不影响状态显示）
        self._schedule_health_check()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 样式 ----------
    def _setup_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TFrame", background=C["bg"])
        s.configure("Card.TFrame", background=C["card"])
        s.configure("TLabel", background=C["bg"], foreground=C["text"], font=FONT_UI)
        s.configure("Card.TLabel", background=C["card"], foreground=C["text"], font=FONT_UI)
        s.configure("CardMuted.TLabel", background=C["card"],
                    foreground=C["muted"], font=FONT_UI)
        s.configure("Title.TLabel", background=C["bg"],
                    foreground="#ffffff", font=("Microsoft YaHei UI", 15, "bold"))
        s.configure("Accent.TLabel", background=C["card"],
                    foreground=C["accent"], font=("Microsoft YaHei UI", 10, "bold"))
        s.configure("TButton", background=C["card"], foreground=C["text"],
                    bordercolor=C["border"], focuscolor=C["accent"], font=FONT_UI)
        s.map("TButton",
              background=[("active", C["border"]), ("disabled", C["card"])],
              foreground=[("disabled", C["muted"])])
        s.configure("Primary.TButton", background=C["accent"],
                    foreground="#ffffff", font=("Microsoft YaHei UI", 10, "bold"))
        s.map("Primary.TButton",
              background=[("active", "#5a8fdf"), ("disabled", C["border"])],
              foreground=[("disabled", C["muted"])])
        s.configure("Danger.TButton", background=C["red"],
                    foreground="#ffffff", font=("Microsoft YaHei UI", 10, "bold"))
        s.map("Danger.TButton",
              background=[("active", "#c93f3e"), ("disabled", C["border"])],
              foreground=[("disabled", C["muted"])])

    # ---------- 容器 ----------
    def _card(self, parent) -> tk.Frame:
        """带 1px 边框的卡片，返回 body（可向其中放控件）。"""
        outer = tk.Frame(parent, bg=C["border"], bd=0, highlightthickness=0)
        body = tk.Frame(outer, bg=C["card"])
        body.pack(fill="both", expand=True, padx=1, pady=1)
        outer.body = body
        return outer

    # ---------- 主界面 ----------
    def _build_ui(self):
        # 顶部标题
        header = tk.Frame(self.root, bg=C["bg"])
        header.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(header, text="TOEIC 启动管理器", bg=C["bg"],
                 fg="#ffffff", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        py_name = Path(self.svc.python_exe).name
        tk.Label(header, text=f"Python: {py_name}", bg=C["bg"],
                 fg=C["muted"], font=FONT_UI).pack(side="right")

        # --- 服务控制卡 ---
        ctrl = self._card(self.root)
        ctrl.pack(fill="x", padx=14, pady=4)
        body = ctrl.body

        btn_row = tk.Frame(body, bg=C["card"])
        btn_row.pack(fill="x", padx=12, pady=(10, 4))
        self.btn_start = ttk.Button(btn_row, text="▶  启动", style="Primary.TButton",
                                    command=self.on_start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(btn_row, text="■  停止", style="Danger.TButton",
                                   command=self.on_stop)
        self.btn_stop.pack(side="left", padx=6)
        self.btn_restart = ttk.Button(btn_row, text="↻  重启", command=self.on_restart)
        self.btn_restart.pack(side="left", padx=6)
        ttk.Button(btn_row, text="🌐  浏览器", command=self.open_browser).pack(side="left", padx=6)
        ttk.Button(btn_row, text="📝  编辑 .env", command=self.edit_env).pack(side="left", padx=6)
        ttk.Button(btn_row, text="📂  打开目录", command=self.open_dir).pack(side="right")

        status_row = tk.Frame(body, bg=C["card"])
        status_row.pack(fill="x", padx=12, pady=(4, 10))
        self.dot_status = tk.Label(status_row, text="●", fg=C["muted"],
                                   bg=C["card"], font=("Microsoft YaHei UI", 13))
        self.dot_status.pack(side="left", padx=(0, 6))
        self.lbl_status = tk.Label(status_row, text="未运行", bg=C["card"],
                                   fg=C["accent"],
                                   font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_status.pack(side="left", padx=(0, 16))
        self.lbl_pid = tk.Label(status_row, text="PID: -", bg=C["card"],
                                fg=C["muted"], font=FONT_UI)
        self.lbl_pid.pack(side="left", padx=16)
        self.lbl_uptime = tk.Label(status_row, text="运行: 00:00:00",
                                   bg=C["card"], fg=C["muted"], font=FONT_UI)
        self.lbl_uptime.pack(side="left", padx=16)
        self.lbl_port = tk.Label(status_row, text=f"端口: {PORT}",
                                 bg=C["card"], fg=C["muted"], font=FONT_UI)
        self.lbl_port.pack(side="left", padx=16)

        # --- 前后端状态卡 ---
        svc = self._card(self.root)
        svc.pack(fill="x", padx=14, pady=4)
        svc_row = tk.Frame(svc.body, bg=C["card"])
        svc_row.pack(fill="x", padx=12, pady=10)

        fe = tk.Frame(svc_row, bg=C["card"])
        fe.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Label(fe, text="🖥   前端 Web", bg=C["card"], fg=C["accent"],
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(fe, text=BASE_URL, bg=C["card"], fg=C["muted"],
                 font=FONT_UI).pack(anchor="w")
        self.lbl_fe = tk.Label(fe, text="●  离线", bg=C["card"],
                               fg=C["muted"], font=FONT_UI)
        self.lbl_fe.pack(anchor="w")

        be = tk.Frame(svc_row, bg=C["card"])
        be.pack(side="left", fill="x", expand=True, padx=(6, 0))
        tk.Label(be, text="⚙   后端 API", bg=C["card"], fg=C["accent"],
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(be, text=f"{BASE_URL}/api/health", bg=C["card"],
                 fg=C["muted"], font=FONT_UI).pack(anchor="w")
        self.lbl_be = tk.Label(be, text="●  离线", bg=C["card"],
                               fg=C["muted"], font=FONT_UI)
        self.lbl_be.pack(anchor="w")
        self.lbl_keys = tk.Label(be, text="DeepSeek: ?    TTS: ?    Image: ?",
                                 bg=C["card"], fg=C["muted"], font=FONT_UI)
        self.lbl_keys.pack(anchor="w")

        # 自检按钮（一次性诊断，结果写入下方日志）
        self.btn_health = ttk.Button(
            svc_row, text="▶  自检", style="Primary.TButton",
            command=self._run_self_check, width=12,
        )
        self.btn_health.pack(side="right", padx=(10, 0))

        # --- 日志卡 ---
        log_card = self._card(self.root)
        log_card.pack(fill="both", expand=True, padx=14, pady=(4, 10))
        lb = log_card.body

        log_head = tk.Frame(lb, bg=C["card"])
        log_head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(log_head, text="📋  日志", bg=C["card"], fg=C["accent"],
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")

        filter_row = tk.Frame(lb, bg=C["card"])
        filter_row.pack(fill="x", padx=12, pady=4)
        self.filter_var = tk.StringVar(value="ALL")
        for text, val in [("全部", "ALL"), ("INFO", "INFO"),
                          ("WARNING", "WARNING"), ("ERROR", "ERROR")]:
            tk.Radiobutton(filter_row, text=text, value=val, variable=self.filter_var,
                           bg=C["card"], fg=C["text"],
                           selectcolor=C["card"],
                           activebackground=C["card"],
                           activeforeground=C["text"],
                           font=FONT_UI,
                           command=self._apply_filter).pack(side="left", padx=(0, 10))
        tk.Label(filter_row, text="搜索:", bg=C["card"],
                 fg=C["muted"], font=FONT_UI).pack(side="left", padx=(20, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._apply_filter())
        tk.Entry(filter_row, textvariable=self.search_var,
                 bg=C["bg"], fg=C["text"], insertbackground=C["text"],
                 relief="flat", width=24, font=FONT_UI).pack(side="left", padx=(0, 4))

        self.log_text = scrolledtext.ScrolledText(
            lb, wrap="word",
            bg=C["bg"], fg=C["text"],
            insertbackground=C["text"],
            selectbackground=C["accent"],
            relief="flat", state="disabled",
            font=FONT_MONO, height=18,
            padx=10, pady=8,
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=4)
        self.log_text.tag_config("time", foreground=C["log_time"])
        self.log_text.tag_config("INFO",    foreground=C["text"])
        self.log_text.tag_config("WARNING", foreground=C["yellow"])
        self.log_text.tag_config("ERROR",   foreground=C["red"])
        self.log_text.tag_config("DEBUG",   foreground=C["muted"])

        footer = tk.Frame(lb, bg=C["card"])
        footer.pack(fill="x", padx=12, pady=(4, 10))
        self.autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(footer, text="自动滚动", variable=self.autoscroll_var,
                       bg=C["card"], fg=C["text"], selectcolor=C["card"],
                       activebackground=C["card"], activeforeground=C["text"],
                       font=FONT_UI,
                       command=self._toggle_autoscroll).pack(side="left")
        self.lbl_count = tk.Label(footer, text="0 行", bg=C["card"],
                                  fg=C["muted"], font=FONT_UI)
        self.lbl_count.pack(side="left", padx=(16, 0))
        ttk.Button(footer, text="保存到文件", command=self.save_logs).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="复制", command=self.copy_logs).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="清空", command=self.clear_logs).pack(side="right", padx=(6, 0))

    # ---------- 服务控制 ----------
    def on_start(self):
        ok, msg = self.svc.start()
        self._log_system(msg)
        if ok:
            self._log_system(f"Python: {self.svc.python_exe}")
            self._log_system(f"工作目录: {MVP_DIR}")
            self._log_system(f"主程序: {MAIN_PY.name}")
            self._log_system(f"访问地址: {BASE_URL}")
        self._update_running_ui(ok)

    def on_stop(self):
        ok, msg = self.svc.stop()
        self._log_system(msg)
        self._update_running_ui(False)

    def on_restart(self):
        ok, msg = self.svc.restart()
        self._log_system(msg)
        if ok:
            self._log_system("--- 重启完成 ---")
        self._update_running_ui(ok)

    def _update_running_ui(self, running: bool):
        if running:
            self.dot_status.config(fg=C["green"])
            self.lbl_status.config(text="运行中")
            self.btn_start.state(["disabled"])
            self.btn_stop.state(["!disabled"])
        else:
            self.dot_status.config(fg=C["muted"])
            self.lbl_status.config(text="未运行")
            self.btn_start.state(["!disabled"])
            self.btn_stop.state(["disabled"])

    def _log_system(self, msg: str):
        self._log_line("INFO", msg)

    def _log_line(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append((ts, level, msg))
        self._render_logs()

    def clear_logs(self):
        self.log_lines.clear()
        self._rendered_count = 0
        self._full_rebuild = True
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.lbl_count.config(text="0 行")

    # ---------- 日志渲染 ----------
    def _poll_log_queue(self):
        """从日志队列批量取出新行，增量渲染。"""
        new_lines = 0
        try:
            while True:
                ts, level, line = self.svc.log_queue.get_nowait()
                self.log_lines.append((ts, level, line))
                new_lines += 1
                if len(self.log_lines) > MAX_LOG_LINES:
                    self.log_lines = self.log_lines[-TRIM_TO:]
                    self._rendered_count = 0  # 裁剪后重建
                    self._full_rebuild = True
        except queue.Empty:
            pass

        if new_lines > 0 or self._full_rebuild:
            self._render_logs()
        self.root.after(500, self._poll_log_queue)  # 从 150ms → 500ms

    def _apply_filter(self):
        self.filter_level = self.filter_var.get()
        self.search_text = self.search_var.get().strip().lower()
        self._full_rebuild = True

    def _toggle_autoscroll(self):
        self.autoscroll = self.autoscroll_var.get()

    def _render_logs(self):
        if self.filter_level == "ALL" and not self.search_text:
            self._render_incremental()
        else:
            self._render_filtered()

    def _render_incremental(self):
        """增量追加新日志行（无过滤/搜索时，O(1) 级开销）。"""
        self.log_text.config(state="normal")
        for i in range(self._rendered_count, len(self.log_lines)):
            ts, level, line = self.log_lines[i]
            self.log_text.insert("end", f"[{ts}] ", "time")
            self.log_text.insert("end", f"{level:<8s} ", level)
            self.log_text.insert("end", line + "\n", level)
        self.log_text.config(state="disabled")
        self._rendered_count = len(self.log_lines)
        self._full_rebuild = False
        self.lbl_count.config(text=f"{self._rendered_count} 行")
        if self.autoscroll:
            self.log_text.see("end")

    def _render_filtered(self):
        """完整重绘（过滤/搜索时，仅在条件变化时触发）。"""
        shown = []
        for ts, level, line in self.log_lines:
            if self.filter_level != "ALL" and level != self.filter_level:
                continue
            if self.search_text and self.search_text not in line.lower():
                continue
            shown.append((ts, level, line))

        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        for ts, level, line in shown:
            self.log_text.insert("end", f"[{ts}] ", "time")
            self.log_text.insert("end", f"{level:<8s} ", level)
            self.log_text.insert("end", line + "\n", level)
        self.log_text.config(state="disabled")
        self._full_rebuild = False
        self.lbl_count.config(text=f"{len(shown)} / {len(self.log_lines)} 行")
        if self.autoscroll:
            self.log_text.see("end")

    def copy_logs(self):
        content = self.log_text.get("1.0", "end").strip()
        if content:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)

    def save_logs(self):
        if not self.log_lines:
            messagebox.showinfo("提示", "没有日志可保存", parent=self.root)
            return
        default_name = f"toeic_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        path = filedialog.asksaveasfilename(
            title="保存日志", initialdir=str(LOG_DIR), initialfile=default_name,
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for ts, level, line in self.log_lines:
                    f.write(f"[{ts}] {level:<8s} {line}\n")
            messagebox.showinfo("保存成功", f"日志已保存到:\n{path}", parent=self.root)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self.root)

    # ---------- 手动自检（仅输出到下方日志，不影响常驻状态显示） ----------
    def _run_self_check(self):
        """执行一次手动自检：环境 + 服务连通性，结果写入日志面板。"""
        if self._self_checking:
            return
        self._self_checking = True
        self.btn_health.config(state="disabled")
        self._log_line("INFO", "--- 自检开始 ---")
        threading.Thread(target=self._self_check_worker, daemon=True).start()

    def _self_check_worker(self):
        """后台线程执行各项检查，结果收集后推回主线程写入日志。"""
        report = build_self_check_report(self.svc)
        self.root.after(0, self._apply_self_check_result, report)

    def _apply_self_check_result(self, report):
        """主线程：把自检报告逐行写入日志面板。"""
        for level, msg in report:
            self._log_line(level, msg)
        self.btn_health.config(state="normal")
        self._self_checking = False

    def _schedule_health_check(self):
        """调度一次健康检查（常驻运行，前后端状态始终显示）。"""
        # 主线程快速处理进程状态变化
        if self.svc.process is not None and self.svc.process.poll() is not None:
            self.svc.process = None
            self.svc.start_time = None
            self._update_running_ui(False)

        if self.svc.is_running:
            self.lbl_pid.config(text=f"PID: {self.svc.pid}")
            self.lbl_uptime.config(text=f"运行: {self.svc.uptime}")
            # 后台线程做 HTTP 检查
            threading.Thread(target=self._health_check_worker, daemon=True).start()
        else:
            self.lbl_pid.config(text="PID: -")
            self.lbl_uptime.config(text="运行: 00:00:00")
            self.fe_ok = False
            self.be_ok = False
            self.health_data = None
            self._update_service_status()
            # 服务未运行 → 5 秒后再查
            self.root.after(5000, self._schedule_health_check)

    def _health_check_worker(self):
        """在后台线程中执行 HTTP 健康检查，结果推回主线程。"""
        fe_ok = check_url(BASE_URL, timeout=1.0)
        health_data = check_health() if fe_ok else None
        be_ok = health_data is not None
        self.root.after(0, self._apply_health_result, fe_ok, be_ok, health_data)

    def _apply_health_result(self, fe_ok, be_ok, health_data):
        """在主线程中应用健康检查结果到 UI。"""
        self.fe_ok = fe_ok
        self.be_ok = be_ok
        self.health_data = health_data
        self._update_service_status()
        self.root.after(2000, self._schedule_health_check)

    def _update_service_status(self):
        if self.fe_ok:
            self.lbl_fe.config(text="●  已就绪", fg=C["green"])
        else:
            self.lbl_fe.config(text="●  离线", fg=C["muted"])

        if self.be_ok and self.health_data:
            self.lbl_be.config(text="●  已就绪", fg=C["green"])
            ds  = self.health_data.get("deepseek_key", False)
            tts = self.health_data.get("tts_key", False)
            img = self.health_data.get("image_key", False)
            self.lbl_keys.config(
                text=f"DeepSeek: {'✓ 已配置' if ds else '✗ 未配置'}    "
                     f"TTS: {'✓ 已配置' if tts else '✗ 未配置'}    "
                     f"Image: {'✓ 已配置' if img else '✗ 未配置'}"
            )
        else:
            self.lbl_be.config(text="●  离线", fg=C["muted"])
            self.lbl_keys.config(text="DeepSeek: ?    TTS: ?    Image: ?")

    # ---------- 工具按钮 ----------
    def open_browser(self):
        webbrowser.open(BASE_URL)

    def edit_env(self):
        if not ENV_FILE.exists():
            if messagebox.askyesno("提示", ".env 不存在，是否从 .env.example 复制？",
                                   parent=self.root):
                if ENV_EX.exists():
                    import shutil
                    shutil.copy(ENV_EX, ENV_FILE)
                else:
                    messagebox.showerror("错误", "找不到 .env.example", parent=self.root)
                    return
            else:
                return
        try:
            if sys.platform == "win32":
                os.startfile(str(ENV_FILE))
            else:
                subprocess.run(["xdg-open", str(ENV_FILE)])
        except Exception as e:
            messagebox.showerror("打开失败", str(e), parent=self.root)

    def open_dir(self):
        try:
            if sys.platform == "win32":
                os.startfile(str(MVP_DIR))
            else:
                subprocess.run(["xdg-open", str(MVP_DIR)])
        except Exception as e:
            messagebox.showerror("打开失败", str(e), parent=self.root)

    # ---------- 关闭 ----------
    def _on_close(self):
        if self.svc.is_running:
            if not messagebox.askyesno(
                "确认退出",
                "服务正在运行，关闭管理器将一并停止服务。\n是否继续？",
                parent=self.root,
            ):
                return
            self.svc.stop()
        self.root.destroy()


# ========================================================================
# 终端交互模式 (--cli)
# ========================================================================

ANSI = {
    "RESET":   "\033[0m",
    "DIM":     "\033[2m",
    "TIME":    "\033[2m\033[37m",
    "MUTED":   "\033[2m\033[90m",
    "INFO":    "\033[37m",
    "WARNING": "\033[33m",
    "ERROR":   "\033[31m",
    "DEBUG":   "\033[90m",
    "GREEN":   "\033[32m",
    "CYAN":    "\033[36m",
}


def _cli_emit(level: str, line: str):
    """按级别着色输出一行日志。"""
    color = ANSI.get(level, ANSI["INFO"])
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ANSI['TIME']}[{ts}]{ANSI['RESET']} {color}{level:<8s}{ANSI['RESET']} {line}",
          flush=True)


def _cli_drain(svc: ServiceManager):
    """把服务日志队列中的新行全部输出。"""
    while True:
        try:
            ts, level, line = svc.log_queue.get_nowait()
        except queue.Empty:
            return
        _cli_emit(level, line)


def _cli_key_listener(key_queue: queue.Queue, done: threading.Event):
    """单键输入监听（Windows: msvcrt 无回显；其他平台: input 回车确认）。"""
    if sys.platform == "win32":
        import msvcrt
        while not done.is_set():
            try:
                ch = msvcrt.getwch()
            except Exception:
                break
            key_queue.put(ch.lower())
    else:
        while not done.is_set():
            try:
                ch = input()
            except (EOFError, KeyboardInterrupt):
                break
            if ch:
                key_queue.put(ch[0].lower())


def run_cli():
    """终端交互模式：tail -f 滚动日志 + 单键命令。"""
    if sys.platform == "win32":
        os.system("")  # 启用 Windows 控制台 ANSI 转义序列

    svc = ServiceManager()
    key_queue: queue.Queue = queue.Queue()
    done = threading.Event()
    state = {"fe": False, "be": False, "health": None, "checking": False}
    last_status = 0.0

    def status_line():
        if svc.is_running:
            core = (f"{ANSI['GREEN']}● 运行中{ANSI['RESET']} "
                    f"PID={svc.pid} 运行={svc.uptime}")
        else:
            core = f"{ANSI['MUTED']}● 未运行{ANSI['RESET']} PID=- 运行=00:00:00"
        fe = (f"{ANSI['GREEN']}已就绪{ANSI['RESET']}" if state["fe"]
              else f"{ANSI['MUTED']}离线{ANSI['RESET']}")
        be = (f"{ANSI['GREEN']}已就绪{ANSI['RESET']}" if state["be"]
              else f"{ANSI['MUTED']}离线{ANSI['RESET']}")
        h = state["health"] or {}
        if state["be"]:
            keys = (f"DeepSeek={'✓' if h.get('deepseek_key') else '✗'}    "
                    f"TTS={'✓' if h.get('tts_key') else '✗'}    "
                    f"Image={'✓' if h.get('image_key') else '✗'}")
        else:
            keys = "DeepSeek: ?    TTS: ?    Image: ?"
        print(f"{ANSI['CYAN']}── {core} | 前端={fe} 后端={be} | {keys}{ANSI['RESET']}",
              flush=True)

    def banner():
        print()
        print("=" * 60)
        print("  TOEIC 启动管理器 (CLI)")
        print("=" * 60)
        print(f"  前端: {BASE_URL}")
        print(f"  Python: {Path(svc.python_exe).name}")
        print()
        print("  按键: [s]启动  [x]停止  [r]重启  [c]自检  [b]浏览器  [q]退出")
        print("  GUI 模式: 运行 manager.bat gui")
        print()

    def do_start():
        ok, msg = svc.start()
        _cli_emit("INFO", msg)
        if ok:
            _cli_emit("INFO", f"访问地址: {BASE_URL}")
        status_line()

    def do_stop():
        ok, msg = svc.stop()
        _cli_emit("INFO", msg)
        status_line()

    def do_restart():
        ok, msg = svc.restart()
        _cli_emit("INFO", msg)
        if ok:
            _cli_emit("INFO", "--- 重启完成 ---")
        status_line()

    def do_check():
        if state["checking"]:
            return
        state["checking"] = True

        def worker():
            try:
                _cli_emit("INFO", "--- 自检开始 ---")
                for level, msg in build_self_check_report(svc):
                    _cli_emit(level, msg)
                _cli_emit("INFO", "--- 自检完成 ---")
            finally:
                state["checking"] = False

        threading.Thread(target=worker, daemon=True).start()

    def do_quit():
        done.set()

    keys = {
        "s": do_start,
        "x": do_stop,
        "r": do_restart,
        "c": do_check,
        "b": lambda: webbrowser.open(BASE_URL),
        "q": do_quit,
    }

    def health_loop():
        while not done.is_set():
            fe = check_url(BASE_URL, timeout=1.0)
            health = check_health() if fe else None
            state["fe"] = fe
            state["be"] = health is not None
            state["health"] = health
            time.sleep(5)

    banner()
    threading.Thread(target=health_loop, daemon=True).start()
    threading.Thread(target=_cli_key_listener, args=(key_queue, done),
                     daemon=True).start()

    try:
        while not done.is_set():
            try:
                while True:
                    ch = key_queue.get_nowait()
                    action = keys.get(ch)
                    if action:
                        action()
            except queue.Empty:
                pass

            # 服务进程意外退出时同步状态
            if svc.process is not None and svc.process.poll() is not None:
                svc.process = None
                svc.start_time = None

            now = time.time()
            if now - last_status >= 5.0:
                status_line()
                last_status = now

            _cli_drain(svc)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        if svc.is_running:
            _cli_emit("INFO", "关闭管理器，停止服务...")
            svc.stop()
        print("已退出")


# ========================================================================
# 入口
# ========================================================================

def main():
    if "--cli" in sys.argv:
        run_cli()
        return
    try:
        app = ManagerApp(tk.Tk())
        app.root.mainloop()
    except Exception as e:
        import traceback
        print(f"\n[X] 启动管理器失败: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
