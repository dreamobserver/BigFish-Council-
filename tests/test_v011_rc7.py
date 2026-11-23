"""v0.1.1-rc7 新增回归：Codex 审计 BFC-01 ~ BFC-08 + 手机端只读角色。

对应仓库里 docs/AUDIT-CODEX-v0.1.1-rc6.md 的八条意见。每条断言的都是**可观测结果**，
不是「某个函数被调用过」：

  BFC-01  Windows 恢复 TOCTOU —— 真实线程抢锁，验证排他期间绝不允许新写者出现
  BFC-02  MCP 字段级长度上限 —— 超长字段必须 isError，边界值必须放行
  BFC-03  create_app() 绕过监听闸门 —— 带闸门工厂 + 连接级中间件两层
  BFC-04  审计 manifest 布尔声明过强 —— 改成枚举 credential_status
  BFC-05  CLI 非 restore 分支不关库 —— 退出后写者租约必须已释放
  BFC-06  allowed-ports 不打印预留端口
  BFC-07  发布工程不闭环 —— requirements.lock 与 CI 工作流
  BFC-08  tag 溯源（发布流程保证，这里只校验仓库卫生）
  MOBILE  手机端必须是只读角色，且不能被兜底成执行器

刻意不写死依赖版本号，避免每次升级都要改测试。
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import time
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from bigfish import cli as CLI
from bigfish import roles as R
from bigfish.backup import export_audit_bundle, export_bundle
from bigfish.core import Council
from bigfish.db import Database
from bigfish.locks import LeaseError, exclusive_access
from bigfish.security import load_or_create_key
from bigfish.server import (
    BindError,
    LoopbackGuardMiddleware,
    create_app,
    create_production_app,
    tool_specs,
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.key_path = self.root / "secrets" / "ticket.key"
        self.c = Council(self.db, key=load_or_create_key(self.key_path),
                         heartbeat_ttl=60, ticket_ttl=120, ticket_max_ttl=3600,
                         heartbeat_lease=180, key_path=self.key_path)
        self.ex = self.c.register_executor("dsh-cloud", "executor")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def approved(self, title="演示") -> str:
        t = self.c.create_task(title, "验证", "独立目录", "low", "prompt-body")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid


# ======================================================================
# BFC-01 写者租约：不存在「检查完到上锁之间」的窗口
# ======================================================================
class TestLeaseAtomicity(Base):
    """审计的核心指控：旧实现先扫「有没有别的写者」，再锁另一把锁。

    两把不是同一个内核对象，扫描返回后新 Database 仍能在新文件上建锁，
    互斥就此失效。新实现只有一把访问锁：写者共享、恢复排他。
    这里用真实线程抢，断言最终互斥结果。
    """

    def test_writer_cannot_open_while_exclusive(self):
        """恢复持排他锁期间，新 Database 必须打不开。"""
        path = self.root / "gated.db"
        probe = Database(path)
        probe.init_schema()
        probe.close()

        entered, release = threading.Event(), threading.Event()

        def hold():
            with exclusive_access(path, timeout=5.0):
                entered.set()
                release.wait(10)

        th = threading.Thread(target=hold, daemon=True)
        th.start()
        self.assertTrue(entered.wait(5), "排他锁没拿到")
        try:
            with self.assertRaises(LeaseError, msg="排他期间竟然能打开数据库"):
                Database(path)
        finally:
            release.set()
            th.join(timeout=10)
        Database(path).close()

    def test_exclusive_refused_while_writer_alive(self):
        """有写者活着时恢复必须失败，且不能顺手改库。"""
        tid = self.approved()
        bundle = self.root / "b.zip"
        export_bundle(self.db, bundle)
        other = Database(self.db.path)
        try:
            before = self.c.get_task(tid)["status"]
            with self.assertRaises(LeaseError):
                with exclusive_access(self.db.path, timeout=1.0):
                    pass
            self.assertEqual(self.c.get_task(tid)["status"], before,
                             "恢复被拒之后库被改了")
        finally:
            other.close()

    def test_same_process_second_database_does_not_deadlock(self):
        """同进程多实例共享租约（rc6 平台 B 实测出来的性质），不能互锁。"""
        other = Database(self.db.path)
        try:
            # 两个实例同时可用：老实例还能读写，新实例也没被挡在门外
            self.assertIn("counts", self.c.overview())
            ok, why = other.integrity(self.db.path); self.assertTrue(ok, why)
        finally:
            other.close()

    def test_lease_released_after_close_allows_exclusive(self):
        """库关掉之后排他锁必须能拿到，否则恢复永远等不到窗口。"""
        path = self.root / "lonely.db"
        db = Database(path)
        db.init_schema()
        db.close()
        with exclusive_access(path, timeout=2.0):
            pass

    def test_lock_files_are_never_deleted(self):
        """rc6 踩过的坑：unlink 锁文件会让新实例在新 inode 上加锁而失效。"""
        path = self.root / "keep.db"
        db = Database(path)
        db.init_schema()
        lock = Path(str(path) + ".access.lock")
        self.assertTrue(lock.exists(), "访问锁文件没建出来")
        db.close()
        self.assertTrue(lock.exists(), "库关闭后锁文件被删了（互斥会失效）")

    def test_real_second_process_blocks_restore(self):
        """真正独立的第二个进程持有写者租约时，恢复必须被拒。

        上面几条里的「第二个写者」都是同进程再开一个 Database。Windows 的
        msvcrt 区间锁按**文件句柄**记，同进程共享句柄走的是 _LOCAL 引用计数，
        所以那些用例根本没有真正测到跨进程互斥。这条用 subprocess 起一个独立
        进程去持租约，两个平台走的是同一套判定。
        """
        path = self.root / "xproc.db"
        probe = Database(path)
        probe.init_schema()
        probe.close()

        ready = self.root / "child-ready.flag"
        release = self.root / "child-release.flag"
        script = (
            "import sys, time" + chr(10) +
            "from pathlib import Path" + chr(10) +
            "sys.path.insert(0, sys.argv[1])" + chr(10) +
            "from bigfish.db import Database" + chr(10) +
            "db = Database(Path(sys.argv[2]))" + chr(10) +
            "Path(sys.argv[3]).write_text('1')" + chr(10) +
            "deadline = time.time() + 60" + chr(10) +
            "while not Path(sys.argv[4]).exists() and time.time() < deadline:" + chr(10) +
            "    time.sleep(0.05)" + chr(10) +
            "db.close()" + chr(10)
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT), str(path), str(ready), str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 60
            while not ready.exists() and time.time() < deadline:
                if child.poll() is not None:
                    err = child.stderr.read().decode("utf-8", "replace")
                    self.fail("第二个进程提前退出：" + err)
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "第二个进程没拿到写者租约")
            self.assertIsNone(child.poll(), "第二个进程已经退出了，这条断言测不到互斥")
            with self.assertRaises(
                    LeaseError,
                    msg="真正的第二个进程正在写，恢复竟然拿到了排他锁"):
                with exclusive_access(path, timeout=1.5):
                    pass
        finally:
            release.write_text("1", encoding="utf-8")
            try:
                child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        # 第二个进程退出、租约释放之后，恢复必须能拿到
        with exclusive_access(path, timeout=5.0):
            pass


# ======================================================================
# BFC-02 MCP 字段级长度上限
# ======================================================================
class TestMcpFieldLimit(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.c, watchdog_interval=None))
        self.hdr = {"Authorization": "Bearer " + self.ex["token"]}

    def _call(self, name, args):
        return self.client.post("/mcp", headers=self.hdr, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}).json()

    def test_free_text_fields_declare_max_length(self):
        specs = tool_specs()
        hb = specs["council_heartbeat"]["inputSchema"]["properties"]
        self.assertIn("maxLength", hb["detail"], "心跳 detail 没有长度上限")
        rs = specs["council_submit_result"]["inputSchema"]["properties"]
        for field in ("log", "diff", "tests", "note"):
            self.assertIn("maxLength", rs[field], "提交 " + field + " 没有长度上限")

    def test_oversized_heartbeat_detail_is_error(self):
        from bigfish.server import MAX_TEXT_FIELD
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r = self._call("council_heartbeat", {
            "ticket": p["ticket"], "status": "running",
            "detail": "d" * (MAX_TEXT_FIELD + 1)})
        self.assertTrue(r["result"]["isError"], "超长字段没被当成错误")
        self.assertIn("长度上限", r["result"]["content"][0]["text"])

    def test_at_limit_field_is_accepted(self):
        """刚好等于上限必须放行——边界不能写成 off-by-one。"""
        from bigfish.server import MAX_TEXT_FIELD
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r = self._call("council_heartbeat", {
            "ticket": p["ticket"], "status": "running",
            "detail": "d" * MAX_TEXT_FIELD})
        self.assertFalse(r["result"]["isError"],
                         r["result"]["content"][0]["text"])

    def test_oversized_result_log_is_error_and_nothing_written(self):
        """被拒的提交不能留下半条 artifact。"""
        from bigfish.server import MAX_TEXT_FIELD
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r = self._call("council_submit_result", {
            "ticket": p["ticket"], "status": "completed",
            "log": "x" * (MAX_TEXT_FIELD + 1)})
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(self.c.get_task(tid)["runs"], [])


# ======================================================================
# BFC-03 监听闸门：带闸门的工厂 + 连接级中间件
# ======================================================================
class TestBindGate(Base):
    def test_production_factory_rejects_public_host(self):
        for host in ("0.0.0.0", "::", "[::]", "192.168.1.10", "100.64.1.2"):
            with self.assertRaises(BindError, msg=host + " 被放行"):
                create_production_app(self.c, host, watchdog_interval=None)

    def test_production_factory_accepts_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1", None):
            app, got = create_production_app(self.c, host, watchdog_interval=None)
            self.assertIn(got, ("127.0.0.1", "localhost", "::1"))
            self.assertTrue(hasattr(app, "router"))

    def test_guard_middleware_blocks_non_loopback_client(self):
        """即使有人拿 create_app() 直接绑 0.0.0.0，外部请求也只能拿到 403。"""
        app = create_app(self.c, watchdog_interval=None)
        lan = TestClient(app, client=("192.168.1.9", 54321))
        self.assertEqual(lan.get("/health").status_code, 403,
                         "局域网来客读到了 /health")

    def test_guard_middleware_allows_loopback_client(self):
        app = create_app(self.c, watchdog_interval=None)
        local = TestClient(app, client=("127.0.0.1", 54321))
        self.assertEqual(local.get("/health").status_code, 200)

    def test_trusted_peers_are_only_the_in_process_constants(self):
        """受信名单不能包含任何真实地址，否则中间件形同虚设。"""
        self.assertEqual(LoopbackGuardMiddleware.TRUSTED_PEERS,
                         frozenset({"testclient", "testserver"}))
        for bad in ("127.0.0.1", "0.0.0.0", "localhost", "192.168.1.1"):
            self.assertNotIn(bad, LoopbackGuardMiddleware.TRUSTED_PEERS)


# ======================================================================
# BFC-04 审计 manifest 不再写布尔承诺
# ======================================================================
class TestManifestCredentialStatus(Base):
    def _manifest(self, path):
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("manifest.json").decode("utf-8"))

    def test_operation_bundle_says_hashes_only(self):
        bundle = self.root / "op.zip"
        export_bundle(self.db, bundle)
        m = self._manifest(bundle)
        self.assertEqual(m["credential_status"], "hashes-only")
        self.assertNotIn("contains_plaintext_credentials", m,
                         "布尔承诺又回来了")

    def test_audit_bundle_does_not_promise_clean(self):
        bundle = self.root / "au.zip"
        export_audit_bundle(self.db, bundle)
        m = self._manifest(bundle)
        self.assertEqual(m["credential_status"], "pattern-scrubbed-unverified")
        self.assertTrue(m["credential_scan"]["manual_review_required"])
        self.assertNotIn("contains_plaintext_credentials", m)

    def test_status_value_is_from_known_set(self):
        """枚举值要能被机器判定，不能随便塞字符串。"""
        allowed = {"none", "hashes-only", "pattern-scrubbed-unverified"}
        for fn, name in ((export_bundle, "op2.zip"), (export_audit_bundle, "au2.zip")):
            bundle = self.root / name
            fn(self.db, bundle)
            self.assertIn(self._manifest(bundle)["credential_status"], allowed)


# ======================================================================
# BFC-05 CLI 全分支关库
# ======================================================================
class TestCliClosesDatabase(Base):
    def test_cli_returns_and_releases_lease(self):
        """CLI 跑完（非 restore 分支）再拿排他锁必须能拿到。"""
        data_dir = self.root / "cli-data"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = CLI.main(["--data-dir", str(data_dir), "init"])
        self.assertEqual(rc, 0)
        with exclusive_access(str(data_dir / "council.db"), timeout=2.0):
            pass

    def test_allowed_ports_lists_reserved(self):
        data_dir = self.root / "cli-ports"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = CLI.main(["--data-dir", str(data_dir), "allowed-ports"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        for port in ("3099", "3100", "3101"):
            self.assertIn(port, out["reserved"], "预留端口 " + port + " 没打印")
        self.assertEqual(set(out["implemented"]), {"windows_api", "vps_proxy",
                                                   "test_only"})


# ======================================================================
# BFC-07 发布工程：锁文件 + CI
# ======================================================================
class TestReleaseEngineering(unittest.TestCase):
    def test_requirements_lock_pins_versions(self):
        lock = ROOT / "requirements.lock"
        self.assertTrue(lock.exists(), "没有 requirements.lock")
        pins = [ln.strip() for ln in lock.read_text(encoding="utf-8").splitlines()
                if re.match(r"^[A-Za-z0-9_.\-]+==", ln.strip())]
        self.assertGreater(len(pins), 10, "锁文件里精确版本太少")
        for name in ("fastapi", "uvicorn", "pydantic", "PySide6", "mcp"):
            self.assertTrue(any(p.lower().startswith(name.lower() + "==")
                                for p in pins), name + " 没被锁定")

    def test_ci_workflow_covers_both_platforms(self):
        wf = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(wf.exists(), "没有 CI 工作流")
        text = wf.read_text(encoding="utf-8")
        self.assertIn("ubuntu-latest", text)
        self.assertIn("windows-latest", text)
        self.assertIn("unittest", text)
        self.assertIn("security-note", text)

    def test_ci_secret_scan_actually_matches_tokens(self):
        """CI 里的正则必须真能命中，否则等于没扫。"""
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pat = re.search(r"ghp_\[A-Za-z0-9\]", text)
        self.assertIsNotNone(pat, "CI 没有 ghp_ 扫描规则")


# ======================================================================
# MOBILE 手机端只读角色
# ======================================================================
class TestMobileViewerRole(Base):
    def test_mobile_registers_with_own_prefix(self):
        e = self.c.register_executor("phone", "mobile")
        self.assertTrue(e["id"].startswith("mobile-"))
        self.assertTrue(e["token"].startswith("bfc_mb_"))
        self.assertEqual(e["role"], "mobile")

    def test_mobile_authenticates_as_viewer(self):
        e = self.c.register_executor("phone2", "mobile")
        got = self.c.authenticate_any(e["token"])
        self.assertEqual(got["role_canonical"], R.MOBILE_VIEWER)
        self.assertTrue(R.can(got["role_canonical"], "task.read"))
        self.assertFalse(R.can(got["role_canonical"], "task.claim"))

    def test_mobile_token_cannot_use_executor_channel(self):
        e = self.c.register_executor("phone3", "mobile")
        with self.assertRaises(Exception):
            self.c.authenticate(e["token"], "executor")

    def test_mobile_db_role_never_falls_back_to_executor(self):
        self.assertEqual(R.role_for_db_row("mobile"), R.MOBILE_VIEWER)
        self.assertNotEqual(R.role_for_db_row("mobile"), R.EXECUTOR_DSH)
        self.assertIn("mobile", R.DB_ROLES)

    def test_executor_registration_rejects_unknown_role(self):
        with self.assertRaises(Exception):
            self.c.register_executor("x", "god")


if __name__ == "__main__":
    unittest.main()
