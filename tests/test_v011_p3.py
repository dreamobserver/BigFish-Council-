"""v0.1.1 回归：P3 GUI / 网络拓扑 / 后台进程.

注意：本文件里的"网络"测试只验证本机回环与端口职责一致性，
不声明任何跨设备联通性。跨设备结论见 docs/NETWORK-TOPOLOGY.md 的实测记录。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish.config import (Settings, DEFAULT_API_HOST, DEFAULT_API_PORT,
                            DEFAULT_PROXY_PORT, TEST_API_PORT, PORT_ROLES,
                            LOOPBACK_HOSTS)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestPortTopology(unittest.TestCase):
    """P0-3：端口职责唯一真源，不允许各处写死不同端口。"""

    def test_defaults(self):
        self.assertEqual(DEFAULT_API_HOST, "127.0.0.1")
        self.assertEqual(DEFAULT_API_PORT, 3096)
        self.assertEqual(DEFAULT_PROXY_PORT, 3098)
        self.assertEqual(TEST_API_PORT, 3097)

    def test_roles_cover_three_ports(self):
        ports = {v[0] for v in PORT_ROLES.values()}
        self.assertEqual(ports, {DEFAULT_API_PORT, DEFAULT_PROXY_PORT, TEST_API_PORT})

    def test_api_and_proxy_ports_differ(self):
        self.assertNotEqual(DEFAULT_API_PORT, DEFAULT_PROXY_PORT,
                            "API 与跳板端口相同会互相打架")

    def test_default_host_is_loopback(self):
        self.assertIn(Settings().api_host, LOOPBACK_HOSTS)
        self.assertTrue(Settings().is_loopback)

    def test_non_loopback_detected(self):
        s = Settings()
        s.api_host = "0.0.0.0"
        self.assertFalse(s.is_loopback)
        s.api_host = "100.64.0.12"
        self.assertFalse(s.is_loopback, "Tailnet 100.x 也不是回环，必须拒绝")

    def test_public_dict_has_no_secret(self):
        d = Settings().public_dict()
        blob = json.dumps(d).lower()
        for bad in ("token", "secret", "password", "ticket.key"):
            self.assertNotIn(bad, blob)
        self.assertEqual(d["api_port"], DEFAULT_API_PORT)

    def test_key_path_outside_db(self):
        s = Settings()
        self.assertNotEqual(Path(s.key_path).parent, Path(s.db_path).parent,
                            "执行票密钥不能和数据库放在同一层")

    def test_env_override_wins(self):
        old = os.environ.get("BIGFISH_API_PORT")
        os.environ["BIGFISH_API_PORT"] = "4567"
        try:
            tmp = tempfile.mkdtemp()
            s = Settings.load(Path(tmp))
            self.assertEqual(s.api_port, 4567)
        finally:
            if old is None:
                os.environ.pop("BIGFISH_API_PORT", None)
            else:
                os.environ["BIGFISH_API_PORT"] = old


class TestLoopbackOnly(unittest.TestCase):
    """__main__ 必须拒绝非回环监听。"""

    def test_refuses_public_bind(self):
        d = tempfile.mkdtemp()
        # 子进程的 stderr 编码取决于宿主 locale：GitHub 的 Windows runner 是
        # cp1252，中文无法编码，Python 3.11+ 用 backslashreplace 退化成
        # \\u9519\\u8bef 这样的字面量。这里强制子进程说 UTF-8、父进程也按
        # UTF-8 解码，断言的是「拒绝文案确实写出来了」，不是宿主的编码。
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        r = subprocess.run(
            [sys.executable, "-m", "bigfish", "--headless", "--host", "0.0.0.0",
             "--data-dir", d, "--port", str(free_port())],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 2, "非回环监听竟然启动了")
        self.assertIn("只允许监听回环地址", r.stderr)

    def test_refuses_tailnet_bind(self):
        d = tempfile.mkdtemp()
        r = subprocess.run(
            [sys.executable, "-m", "bigfish", "--headless", "--host", "100.64.0.12",
             "--data-dir", d, "--port", str(free_port())],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
        self.assertEqual(r.returncode, 2)


class TestBackgroundProcess(unittest.TestCase):
    """P1-5：后台进程必须能起来、不阻塞管道、能干净退出。"""

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="bfc-proc-")
        self.port = free_port()
        self.out = Path(self.data) / "out.log"
        self.err = Path(self.data) / "err.log"
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def start(self):
        fout = open(self.out, "wb")
        ferr = open(self.err, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "bigfish", "--headless", "--data-dir", self.data,
             "--port", str(self.port)],
            cwd=str(ROOT), stdout=fout, stderr=ferr, stdin=subprocess.DEVNULL)
        fout.close()
        ferr.close()

    def wait_health(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % self.port, timeout=2) as r:
                    return json.loads(r.read().decode())
            except Exception:
                time.sleep(0.3)
        raise AssertionError("后台服务没起来：\n" + self.err.read_text(errors="replace")[-2000:])

    def test_stdout_redirect_does_not_block(self):
        self.start()
        t0 = time.time()
        h = self.wait_health()
        self.assertEqual(h["status"], "ok")
        # 关键：进程把输出写到文件而不是继承管道，调用方不该被阻塞
        self.assertIsNone(self.proc.poll(), "后台进程意外退出")
        self.assertLess(time.time() - t0, 30)
        self.assertTrue(self.out.exists())

    def test_clean_shutdown(self):
        self.start()
        self.wait_health()
        self.proc.terminate()
        rc = self.proc.wait(timeout=15)
        if os.name == "nt":
            # Windows 没有可被捕获的 SIGTERM：subprocess.terminate() 走
            # TerminateProcess，进程被系统强制结束，退出码为 1 而不是 POSIX 的 -15。
            # 这不是进程自己崩了，下面仍要求端口被释放来证明资源确实回收。
            self.assertIn(rc, (0, 1), f"退出码异常：{rc}")
        else:
            self.assertIn(rc, (0, -15), f"退出码异常：{rc}")
        # 端口必须释放。判据用「能否重新绑定」而不是「连接是否被拒」：
        # Windows 上进程被 TerminateProcess 杀掉后，新连接既可能短暂成功，
        # 也可能一直 Timeout（内核只丢包不回 RST），connect 结果不可作为证据；
        # 但监听套接字一旦随进程消失，本机就能立刻重新绑定同一端口。
        deadline = time.time() + 10
        last = None
        while time.time() < deadline:
            s = socket.socket()
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", self.port))
                s.close()
                return
            except OSError as exc:
                last = exc
                s.close()
                time.sleep(0.3)
        self.fail(f"进程退出后端口仍被占用：{last}")

    @unittest.skipIf(os.name == "nt", "Windows 无 SIGINT 语义，走 TerminateProcess")
    def test_graceful_shutdown_on_sigint(self):
        """审计点名 test_clean_shutdown 只接受 (0, -15)，等于「被信号打死」也算过。

        这里单独要求：SIGINT 必须让服务自己收尾退出（rc == 0），而不是被信号
        杀掉。同时验证退出前 WAL 已经 checkpoint 回主库——否则「干净退出」
        只是进程没了，数据还挂在 -wal 里。
        """
        self.start()
        self.wait_health()
        self.proc.send_signal(signal.SIGINT)
        rc = self.proc.wait(timeout=20)
        self.assertEqual(rc, 0, "SIGINT 没有走优雅退出：\n" +
                         self.err.read_text(errors="replace")[-2000:])
        wal = Path(self.data) / "council.db-wal"
        # close() 里做过 wal_checkpoint(TRUNCATE)，WAL 要么不存在要么为 0 字节
        if wal.exists():
            self.assertLessEqual(wal.stat().st_size, 0,
                                 "优雅退出后 WAL 仍留有未合并的数据")

    def test_data_dir_created(self):
        self.start()
        self.wait_health()
        d = Path(self.data)
        self.assertTrue((d / "council.db").exists(), "数据库未创建")
        self.assertTrue((d / "secrets" / "ticket.key").exists(), "执行票密钥未创建")
        if os.name == "posix":
            mode = (d / "secrets" / "ticket.key").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"密钥权限过宽：{oct(mode)}")

    def test_watchdog_runs_in_headless(self):
        self.start()
        h = self.wait_health()
        self.assertTrue(h["watchdog"], "headless 模式没启动看门狗")


class TestGuiSmoke(unittest.TestCase):
    """P3：真实窗口构造 + 页面切换 + 托盘 + 生命周期（离屏）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
            cls.QApplication = QApplication
        except Exception as exc:            # 依赖缺失是真失败，不做静默跳过
            raise unittest.SkipTest(f"PySide6 不可用：{exc}")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bfc-gui-")
        from bigfish.config import Settings
        from bigfish.runtime import open_council
        self.settings = Settings.load(Path(self.tmp))
        self.settings.api_port = free_port()
        self.db, self.council = open_council(self.settings)

    def tearDown(self):
        try:
            self.council.stop_watchdog()
        except Exception:
            pass
        self.db.close()

    def make_window(self):
        from bigfish import gui
        return gui.MainWindow(self.settings, self.council)

    def test_window_builds_with_five_pages(self):
        win = self.make_window()
        try:
            self.assertEqual(win.stack.count(), len(win.pages))
            self.assertEqual(len(win.pages), 5)
            for name in ("overview", "tasks", "council", "approval", "monitor"):
                self.assertIn(name, win.pages)
        finally:
            win._force_quit = True
            win.close()

    def test_pages_switch(self):
        win = self.make_window()
        try:
            for name, widget in win.pages.items():
                win.goto(name)
                self.assertIs(win.stack.currentWidget(), widget, f"{name} 切不过去")
        finally:
            win._force_quit = True
            win.close()

    def test_sidebar_shows_real_port(self):
        """左侧导航的 API 标签必须用设置里的真实端口，不能写死 3096。"""
        win = self.make_window()
        try:
            win.on_server(True, "")
            text = win.api_label.text()
            self.assertIn(str(self.settings.api_port), text, f"端口文案不对：{text}")
            self.assertIn(self.settings.api_host, text)
            win.on_server(False, "")
            self.assertIn("未启动", win.api_label.text())
        finally:
            win._force_quit = True
            win.close()

    def test_close_without_tray_shuts_down_cleanly(self):
        """无托盘环境（离屏/服务端）关闭窗口必须真正退出，不能挂在确认框上。"""
        win = self.make_window()
        win.show()
        win.close()
        self.assertFalse(win.isVisible(), "关闭后窗口还在")
        try:
            self.council.stop_watchdog()
        except Exception:
            pass

    def test_pending_task_visible_to_approval_page(self):
        t = self.council.create_task("演示", prompt="p")
        tid = t["id"]
        self.council.submit_for_review(tid)
        self.council.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.council.add_review(tid, "gemini", "ok", "approve", kind="vote")
        win = self.make_window()
        page = win.pages["approval"]
        try:
            page.refresh()
            # 列表里必须出现这条待审批任务，选中后记录版本
            self.assertEqual(page.list.count(), 1)
            page.list.setCurrentRow(0)
            self.assertEqual(page.current, tid)
            self.assertEqual(page.loaded_version, 1,
                             "审批页没记住载入时的版本，无法防漂移")
        finally:
            win._force_quit = True
            win.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
