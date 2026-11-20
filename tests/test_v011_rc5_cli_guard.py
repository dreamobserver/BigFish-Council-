"""v0.1.1-rc5：CLI restore 必须防住「另一个进程还在写」。

R8 的维护模式只能管住本进程的连接。另一个 headless/GUI 实例仍持有旧连接时，
替换数据库文件在 POSIX 上会让对方继续写旧 inode（数据静默分叉），在 Windows 上
直接失败。所以 CLI restore 先探本机服务端口，除非显式 --force。
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish import cli


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestCliRestoreGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.port = free_port()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *extra):
        return cli.main(["--data-dir", self.dir, "restore", str(Path(self.dir) / "x.zip"),
                         *extra])

    def test_refuses_when_api_port_is_busy(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(8)
        stop = threading.Event()

        def accept_loop():
            srv.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                    conn.close()
                except socket.timeout:
                    continue
                except OSError:
                    return

        th = threading.Thread(target=accept_loop, daemon=True)
        th.start()
        try:
            cfg = Path(self.dir) / "config.json"
            cfg.write_text('{"api_host": "127.0.0.1", "api_port": %d}' % self.port)
            import os
            os.environ["BIGFISH_API_PORT"] = str(self.port)
            try:
                rc = self._run()
            finally:
                os.environ.pop("BIGFISH_API_PORT", None)
            self.assertEqual(rc, 2, "端口被占用时 CLI 仍然继续恢复")
        finally:
            stop.set()
            srv.close()
            th.join(timeout=2)

    def test_force_bypasses_the_guard(self):
        """--force 必须真的放行：端口占着也继续往下走，走到读包那步才失败。

        断言方式是「拿到的是 RestoreError 而不是返回码 2」——如果 --force 只是
        被 argparse 收下但没生效，这里会拿到 2。
        """
        from bigfish.backup import RestoreError
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        import os
        os.environ["BIGFISH_API_PORT"] = str(port)
        try:
            with self.assertRaises(RestoreError):
                self._run("--force")
        finally:
            os.environ.pop("BIGFISH_API_PORT", None)
            srv.close()

    def test_port_busy_helper(self):
        self.assertFalse(cli._port_busy("127.0.0.1", free_port()))
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            self.assertTrue(cli._port_busy("127.0.0.1", srv.getsockname()[1]))
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
