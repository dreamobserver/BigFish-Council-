"""v0.1.1-rc6 回归：跨进程排他、停服确认、审计包如实声明、请求体上限.

对应审计（review/rc5-hardening-20261120）里的 F-05 / F-06 / F-07 / F-09。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish import security as SEC
from bigfish.backup import RestoreError, export_audit_bundle, export_bundle
from bigfish.core import Council
from bigfish.db import Database
from bigfish.locks import LeaseError, WriterLease, exclusive_access
from bigfish.security import load_or_create_key


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.c = Council(self.db, key=load_or_create_key(self.root / "secrets" / "ticket.key"),
                         heartbeat_ttl=60, ticket_ttl=120, ticket_max_ttl=3600,
                         heartbeat_lease=180)
        self.ex = self.c.register_executor("dsh-cloud", "executor")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def approved(self):
        t = self.c.create_task("演示", prompt="prompt-body", goal="g", scope="s")
        tid = t["id"]
        self.c.submit_for_review(tid)
        for model in ("gpt", "gemini"):
            self.c.add_review(tid, model, "ok", verdict="approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid


# ======================================================================
# F-06 跨进程排他
# ======================================================================
class TestCrossProcessLease(Base):
    def test_failed_constructor_does_not_leak_lease(self):
        """版本过高的库打不开时，写者租约必须放掉，否则后面谁也恢复不了。"""
        from bigfish.db import DatabaseVersionError, SCHEMA_VERSION
        path = self.root / "future.db"
        probe = Database(path)
        probe.init_schema()
        with probe.tx() as c:
            c.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                      (str(SCHEMA_VERSION + 1),))
        probe.close()

        with self.assertRaises(DatabaseVersionError):
            Database(path)
        # 打不开之后仍然能拿到排他锁，说明租约没泄漏
        with exclusive_access(path):
            pass

    def test_second_database_on_same_file_blocks_restore(self):
        """另开一个 Database 实例（模拟另一个进程）后，恢复必须被拒。"""
        other = Database(self.db.path)
        try:
            bundle = self.root / "b.zip"
            export_bundle(self.db, bundle)
            with self.assertRaisesRegex(RestoreError, "还有进程在用这个数据库|正在恢复"):
                self.c.restore_from_bundle(bundle)
        finally:
            other.close()

    def test_restore_succeeds_when_no_other_writer(self):
        bundle = self.root / "b.zip"
        self.approved()
        export_bundle(self.db, bundle)
        out = self.c.restore_from_bundle(bundle)
        self.assertTrue(out["restored"])

    def test_lease_is_released_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.db"
            db = Database(path)
            db.init_schema()
            db.close()
            # 关掉之后应该能直接抢到排他锁
            with exclusive_access(path):
                pass

    def test_exclusive_access_fails_while_lease_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "y.db"
            lease = WriterLease(path)
            try:
                with self.assertRaises(LeaseError):
                    with exclusive_access(path):
                        pass
            finally:
                lease.release()

    def test_restore_keeps_lease_after_success(self):
        """恢复完必须把写者租约抢回来，否则别的进程能在我们眼皮底下换库。"""
        bundle = self.root / "b.zip"
        export_bundle(self.db, bundle)
        self.c.restore_from_bundle(bundle)
        with self.assertRaises(LeaseError):
            with exclusive_access(self.db.path):
                pass


# ======================================================================
# F-07 审计包如实声明
# ======================================================================
class TestAuditManifestHonesty(Base):
    def _manifest(self):
        p = self.root / "a.zip"
        export_audit_bundle(self.db, p)
        with zipfile.ZipFile(p) as z:
            return json.loads(z.read("manifest.json"))

    def test_manifest_declares_manual_review_required(self):
        m = self._manifest()
        scan = m["credential_scan"]
        self.assertEqual(scan["mode"], "pattern-based")
        self.assertTrue(scan["manual_review_required"])
        self.assertIn("无法保证", scan["coverage"])
        self.assertIn("google-key", scan["patterns"])

    def test_manifest_does_not_claim_guarantee(self):
        m = self._manifest()
        self.assertNotIn("无凭据", m["handling"])
        self.assertIn("复核", m["handling"])

    def test_new_patterns_scrub_google_slack_stripe(self):
        for sample in ("AIza" + "A" * 35, "xoxb-1234567890-abc", "sk_live_" + "a" * 24):
            self.assertNotEqual(SEC.scrub(sample), sample, f"{sample} 没被脱敏")

    def test_uri_userinfo_is_scrubbed(self):
        out = SEC.scrub("postgres://user:hunter2@db.local:5432/x")
        self.assertNotIn("hunter2", out)

    def test_scan_secrets_reports_kinds(self):
        kinds = SEC.scan_secrets("Authorization: Bearer abcdefghijkl")
        self.assertIn("bearer", kinds)


# ======================================================================
# F-09 请求体上限
# ======================================================================
class TestBodyLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = Database(root / "council.db")
        self.db.init_schema()
        self.c = Council(self.db, key=load_or_create_key(root / "k.key"),
                         heartbeat_ttl=60, ticket_ttl=120, ticket_max_ttl=3600,
                         heartbeat_lease=180)
        from fastapi.testclient import TestClient
        from bigfish.server import create_app
        self.client = TestClient(create_app(self.c, watchdog_interval=None))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_declared_oversize_body_is_rejected(self):
        r = self.client.post("/api/v1/tasks/claim",
                             content=b"x" * 10,
                             headers={"content-length": str(64 * 1024 * 1024)})
        self.assertEqual(r.status_code, 413)

    def test_text_field_limit_is_enforced(self):
        from bigfish.server import MAX_TEXT_FIELD
        ex = self.c.register_executor("dsh-cloud", "executor")
        body = {"ticket": "nope", "status": "running",
                "detail": "d" * (MAX_TEXT_FIELD + 10)}
        r = self.client.post("/api/v1/runs/heartbeat", json=body,
                             headers={"authorization": "Bearer " + ex["token"]})
        self.assertEqual(r.status_code, 422)


# ======================================================================
# F-05 GUI 停服确认（真实线程，不用 TestClient 代替）
# ======================================================================
class TestGuiPauseConfirms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except Exception as exc:
            raise unittest.SkipTest(f"PySide6 不可用：{exc}")
        cls.QApplication = QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from bigfish.config import Settings
        from bigfish.runtime import open_council
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings.load(Path(self.tmp.name))
        self.settings.api_port = 0
        self.db, self.council = open_council(self.settings)

    def tearDown(self):
        try:
            self.council.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def _window(self):
        from bigfish import gui
        return gui.MainWindow(self.settings, self.council)

    def test_stop_raises_when_thread_will_not_exit(self):
        """API 线程卡住时 stop() 必须报错，不能安静返回。"""
        from bigfish.gui import ApiServer

        class Stuck(threading.Thread):
            def run(self):
                time.sleep(3)

        srv = ApiServer(self.council, "127.0.0.1", 0)
        srv.thread = Stuck(daemon=True)
        srv.thread.start()
        try:
            with self.assertRaises(TimeoutError):
                srv.stop(timeout=0.05)
        finally:
            srv.thread.join(5)

    def test_pause_services_stops_api_and_watchdog(self):
        win = self._window()
        try:
            self.council.start_watchdog(1.0)
            win.pause_services()
            self.assertTrue(win._services_paused, "停服后没置 paused")
            self.assertFalse(win.server.is_running(), "API 线程还活着")
            wd = getattr(self.council, "_watchdog", None)
            self.assertTrue(wd is None or not wd.is_alive(), "看门狗还活着")
        finally:
            win._force_quit = True
            win.close()

    def test_pause_is_not_set_when_stop_fails(self):
        """停服失败时不能把 _services_paused 置真。"""
        win = self._window()
        original = win.server.stop
        try:
            win.server.stop = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟停不下来"))
            with self.assertRaisesRegex(RuntimeError, "已中止"):
                win.pause_services()
            self.assertFalse(win._services_paused, "停服失败却置了 paused")
        finally:
            win.server.stop = original
            win.server.stop()
            win._force_quit = True
            win.close()


if __name__ == "__main__":
    unittest.main()
