"""manager.py 的回归测试（unittest，tkinter 窗口会被隐藏）。

覆盖：
  #12 系统日志（_log_system）写入后应能被下一次日志轮询渲染，
      即使服务进程没有任何输出（启动失败等场景）。
  #14 自检报告 build_self_check_report / 密钥读取 read_env_keys
      （GUI 与 CLI 共用，抽为模块级函数后需保证行为一致）。
  #15 终端模式入口 run_cli 与 ANSI 输出函数可用。

运行：python -m unittest test_manager -v
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tkinter as tk

import manager


class ManagerLogRenderTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = manager.ManagerApp(self.root)
        self.app.clear_logs()
        self.app._full_rebuild = False  # 模拟稳定运行状态

    def tearDown(self):
        self.root.destroy()

    def test_system_log_rendered_without_queue_lines(self):
        self.app._log_system("测试系统消息")
        self.assertEqual(len(self.app.log_lines), 1)
        self.assertEqual(self.app._rendered_count, 1)
        rendered = self.app.log_text.get("1.0", "end")
        self.assertIn("测试系统消息", rendered)


class SelfCheckReportTest(unittest.TestCase):
    def test_self_check_report_structure(self):
        svc = mock.Mock()
        svc.python_exe = "python"
        svc.is_running = False
        svc.pid = None
        with mock.patch("manager.subprocess.run",
                        return_value=mock.Mock(stdout="", returncode=0)):
            report = manager.build_self_check_report(svc)
        self.assertIsInstance(report, list)
        self.assertTrue(report)
        for level, msg in report:
            self.assertIn(level, ("GOOD", "BAD", "INFO", "WARNING", "ERROR"))
            self.assertIsInstance(msg, str)
        self.assertIn("Python 解释器", report[0][1])

    def test_read_env_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False,
                                         encoding="utf-8") as f:
            f.write("DEEPSEEK_API_KEY=sk-xxx\nTTS_API_KEY=\n")
            tmp = f.name
        try:
            with mock.patch("manager.ENV_FILE", Path(tmp)):
                keys = manager.read_env_keys()
        finally:
            os.unlink(tmp)
        # 顺序：LLM(百炼) / LLM(DeepSeek) / TTS / ...（智谱廉价模型已移除）
        self.assertEqual(keys[0], ("LLM(百炼)", False))
        self.assertEqual(keys[1], ("LLM(DeepSeek)", True))
        self.assertEqual(keys[2], ("TTS", False))


class CliModeTest(unittest.TestCase):
    def test_cli_functions_exist(self):
        self.assertTrue(callable(manager.run_cli))
        self.assertTrue(callable(manager._cli_emit))
        self.assertTrue(callable(manager._cli_drain))
        self.assertIn("GREEN", manager.ANSI)


if __name__ == "__main__":
    unittest.main()
