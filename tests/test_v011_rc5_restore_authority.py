"""v0.1.1-rc5 新增回归：R1（恢复后授权作废）与 R8（恢复期排空/维护模式）.

这两条必须合在一起验收：
  - R1 决定「恢复后的库里还剩下哪些授权」；
  - R8 决定「替换那一瞬间谁还能写、什么时候可以重新接单」。

只做 R1 仍然会出现「执行器收到成功应答 → 恢复又回滚」的时序；只做 R8
则恢复后的旧凭据依旧能登进来。所以这里每一组都同时验证「授权状态」与
「写入是否被真的拦住」，而不是只看函数返回了什么。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from bigfish import state as S
from bigfish.backup import DISABLED_REASON_RESTORE, export_bundle
from bigfish.core import Council, CouncilError
from bigfish.db import Database, MaintenanceError
from bigfish.security import load_or_create_key
from bigfish.server import create_app


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
        self.ex = self.c.register_executor("dsh-cloud")
        self.rv = self.c.register_executor("gpt", role="reviewer", model="gpt")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def approved(self, title="演示任务") -> str:
        t = self.c.create_task(title, "验证", "独立目录", "low", "prompt v1")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid

    def status(self, tid) -> str:
        return self.c.get_task(tid)["status"]

    def bundle(self, name="bk.zip") -> Path:
        p = self.root / name
        export_bundle(self.db, p)
        return p


# ======================================================================
# R1-1 恢复后所有远端身份一律禁用
# ======================================================================
class TestRestoreDisablesIdentities(Base):
    def test_restore_disables_every_executor(self):
        self.approved()
        bk = self.bundle()
        out = self.c.restore_from_bundle(bk)
        self.assertTrue(out["restored"])
        self.assertGreaterEqual(out["executors_disabled"], 2)

        rows = self.db.query("SELECT enabled, disabled_reason FROM executors")
        self.assertEqual(len(rows), 2, "不许删行，审计要留证据")
        for r in rows:
            self.assertEqual(r["enabled"], 0)
            self.assertEqual(r["disabled_reason"], DISABLED_REASON_RESTORE)

    def test_restored_access_token_no_longer_authenticates(self):
        old_token = self.ex["token"]
        bk = self.bundle()
        self.c.restore_from_bundle(bk)
        # 关键：token 的哈希随快照一起回来了，所以「能查到行」不代表「能认证」。
        # 必须真的被 enabled=0 拒掉，且文案要能指导运维下一步。
        with self.assertRaises(CouncilError) as cm:
            self.c.authenticate(old_token, role="executor")
        msg = str(cm.exception)
        self.assertIn("已被禁用", msg)
        self.assertIn("重新授权", msg)
        self.assertIn("更换凭据", msg)

    def test_reviewer_token_also_rejected_after_restore(self):
        old = self.rv["token"]
        self.c.restore_from_bundle(self.bundle())
        with self.assertRaises(CouncilError) as cm:
            self.c.authenticate(old, role="reviewer")
        self.assertIn("已被禁用", str(cm.exception))


# ======================================================================
# R1-2 重新授权必须同时换凭据
# ======================================================================
class TestReenableRotatesCredential(Base):
    def test_reenable_rotates_token_and_old_one_dies(self):
        old = self.ex["token"]
        self.c.restore_from_bundle(self.bundle())
        new = self.c.reenable_executor(self.ex["id"])
        self.assertNotEqual(new["token"], old, "重新授权必须换掉旧凭据")
        # 新 token 能用
        ex = self.c.authenticate(new["token"], role="executor")
        self.assertEqual(ex["id"], self.ex["id"])
        # 旧 token 必须物理上无法再用（库里哈希已被替换）
        with self.assertRaises(CouncilError):
            self.c.authenticate(old, role="executor")

    def test_reenable_keeps_role_and_model_and_writes_audit(self):
        self.c.restore_from_bundle(self.bundle())
        out = self.c.reenable_executor(self.rv["id"])
        self.assertEqual(out["role"], "reviewer")
        self.assertEqual(out["model"], "gpt")
        acts = [r["action"] for r in self.db.audits(50)]
        self.assertIn("executor.reenabled", acts)

    def test_disabled_reason_cleared_after_reenable(self):
        self.c.restore_from_bundle(self.bundle())
        self.c.reenable_executor(self.ex["id"])
        row = self.db.one("SELECT enabled, disabled_reason FROM executors WHERE id=?",
                          (self.ex["id"],))
        self.assertEqual(row["enabled"], 1)
        self.assertIsNone(row["disabled_reason"])


# ======================================================================
# R1-3 恢复后的库里不留「可领取」的授权
# ======================================================================
class TestRestoreFreezesApproved(Base):
    def test_approved_task_frozen_not_claimable(self):
        tid = self.approved()
        out = self.c.restore_from_bundle(self.bundle())
        self.assertEqual(self.status(tid), S.BLOCKED)
        self.assertIn("冻结", self.db.one(
            "SELECT blocked_reason AS r FROM tasks WHERE id=?", (tid,))["r"])
        self.assertGreaterEqual(out["frozen_approved"], 1)
        # 执行器（即便重新授权过）也领不到。这里必须用认证过的身份，
        # 否则 rc8/R7-02 的角色闸会先一步拒掉，测不到「冻结」这条业务规则。
        ex = self.c.authenticate(
            self.c.reenable_executor(self.ex["id"])["token"], role="executor")
        self.assertEqual(self.c.available_tasks(), [])
        with self.assertRaises(CouncilError) as cm:
            self.c.claim(tid, ex)
        self.assertNotIn("无权", str(cm.exception))

    def test_inflight_task_blocked_and_ticket_revoked(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        bk = self.bundle()
        self.c.restore_from_bundle(bk)
        self.assertEqual(self.status(tid), S.BLOCKED)
        row = self.db.one("SELECT revoked_at FROM tickets WHERE id=?",
                          (p["ticket_id"],))
        self.assertIsNotNone(row["revoked_at"])
        # 旧票就算带着新身份回来也不能用
        self.c.reenable_executor(self.ex["id"])
        new_token = self.c.reenable_executor(self.ex["id"])["token"]
        ex = self.c.authenticate(new_token, role="executor")
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"], ex, "running")

    def test_tickets_revoked_reports_real_count(self):
        """tickets_revoked 必须是真实吊销行数，不是常量。

        rc4 及以前这个字段硬编码为 1，两张活票也只报 1；运维拿它对账会漏。
        """
        tid = self.approved()
        self.c.claim(tid, self.ex)
        second = self.c.create_task("第二张任务", "验证", "独立目录", "low", "p2")
        self.c.submit_for_review(second["id"])
        self.c.add_review(second["id"], "gpt", "ok", "approve", kind="vote")
        self.c.add_review(second["id"], "gemini", "ok", "approve", kind="vote")
        self.c.approve(second["id"], "approve")
        self.c.claim(second["id"], self.ex)
        live = self.db.one(
            "SELECT COUNT(*) AS c FROM tickets WHERE revoked_at IS NULL")["c"]
        self.assertEqual(live, 2)
        bk = self.bundle()
        out = self.c.restore_from_bundle(bk)
        self.assertEqual(out["tickets_revoked"], 2)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) AS c FROM tickets WHERE revoked_at IS NULL")["c"], 0)


    def test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot(self):
        older_snapshot = self.bundle()
        with self.db.tx() as c:
            c.execute("UPDATE meta SET value='7' WHERE key='restore_epoch'")

        out = self.c.restore_from_bundle(older_snapshot)

        self.assertEqual(out["restore_epoch"], 8)
        self.assertEqual(self.db.restore_epoch(), 8)


# ======================================================================
# R1-4 重放路径不能再签发已被撤销的票
# ======================================================================
class TestClaimReplayAfterRestore(Base):
    def test_replay_rejected_after_restore(self):
        tid = self.approved()
        self.c.claim(tid, self.ex, idem_key="k-replay")
        self.c.restore_from_bundle(self.bundle())
        token = self.c.reenable_executor(self.ex["id"])["token"]
        ex = self.c.authenticate(token, role="executor")
        with self.assertRaises(CouncilError):
            self.c.claim(tid, ex, idem_key="k-replay")

    def test_replay_rejected_after_cancel(self):
        tid = self.approved()
        self.c.claim(tid, self.ex, idem_key="k2")
        self.c.request_cancel(tid)
        with self.assertRaises(CouncilError) as cm:
            self.c.claim(tid, self.ex, idem_key="k2")
        self.assertIn("取消", str(cm.exception))


# ======================================================================
# R8 维护模式：排空 + fail-fast
# ======================================================================
class TestMaintenanceMode(Base):
    def test_writes_rejected_inside_maintenance(self):
        with self.db.maintenance("测试维护"):
            self.assertTrue(self.db.in_maintenance())
            with self.assertRaises(MaintenanceError):
                self.c.create_task("维护期间", prompt="p")
            with self.assertRaises(MaintenanceError):
                self.db.query("SELECT 1")
            with self.assertRaises(MaintenanceError):
                with self.db.tx() as c:
                    c.execute("SELECT 1")

    def test_reads_recovered_after_exit(self):
        with self.db.maintenance():
            pass
        self.assertFalse(self.db.in_maintenance())
        t = self.c.create_task("恢复后", prompt="p")
        self.assertEqual(self.status(t["id"]), S.DRAFT)

    def test_maintenance_survives_exception(self):
        with self.assertRaises(RuntimeError):
            with self.db.maintenance():
                raise RuntimeError("boom")
        self.assertFalse(self.db.in_maintenance(), "异常路径也必须退出维护模式")
        self.c.create_task("还能写", prompt="p")

    def test_enter_waits_for_inflight_transaction(self):
        """排空屏障：有写事务在跑时 enter_maintenance 必须等它结束，而不是直接放行。"""
        started = threading.Event()
        release = threading.Event()

        def writer():
            with self.db.tx() as c:
                started.set()
                release.wait(10)
                c.execute("INSERT INTO meta(key,value) VALUES('probe','1')"
                          " ON CONFLICT(key) DO UPDATE SET value='1'")

        th = threading.Thread(target=writer)
        th.start()
        self.assertTrue(started.wait(5))
        err = {}

        def enter():
            try:
                self.db.enter_maintenance("等待排空", timeout=0.3)
            except Exception as exc:
                err["e"] = exc

        th2 = threading.Thread(target=enter)
        th2.start()
        th2.join(5)
        self.assertIsInstance(err.get("e"), MaintenanceError,
                              "有在途写事务时必须拒绝进入，而不是假装排空成功")
        release.set()
        th.join(10)
        # 事务结束后就可以正常进入
        self.db.enter_maintenance("现在可以了", timeout=5)
        self.assertTrue(self.db.in_maintenance())
        self.db.exit_maintenance()

    def _assert_guard_lock_order(self, operation):
        checked = threading.Event()
        release = threading.Event()
        worker_errors = []
        original_guard = self.db._guard
        entered_maintenance_before_operation = False

        def blocked_guard():
            original_guard()
            checked.set()
            if not release.wait(5):
                raise RuntimeError("测试同步等待超时")

        def run_operation():
            try:
                operation()
            except Exception as exc:
                worker_errors.append(exc)

        self.db._guard = blocked_guard
        worker = threading.Thread(target=run_operation, daemon=True)
        try:
            worker.start()
            self.assertTrue(checked.wait(5), "操作没有到达维护检查点")
            try:
                self.db.enter_maintenance("guard/lock race", timeout=0.05)
            except MaintenanceError:
                pass
            else:
                entered_maintenance_before_operation = True
                self.db.exit_maintenance()
        finally:
            release.set()
            worker.join(5)
            self.db._guard = original_guard
            if self.db.in_maintenance():
                self.db.exit_maintenance()

        self.assertFalse(worker.is_alive(), "并发操作线程没有退出")
        self.assertFalse(
            entered_maintenance_before_operation,
            "维护模式在操作已通过 guard、尚未取得锁时插入，说明检查与加锁不是原子的")
        self.assertEqual(worker_errors, [])

    def test_query_guard_is_atomic_with_lock(self):
        self._assert_guard_lock_order(lambda: self.db.query("SELECT 1"))

    def test_transaction_guard_is_atomic_with_lock(self):
        def operation():
            with self.db.tx() as c:
                c.execute("SELECT 1")
        self._assert_guard_lock_order(operation)

    def test_restore_always_exits_maintenance(self):
        self.approved()
        p = self.bundle()
        self.c.restore_from_bundle(p)
        self.assertFalse(self.db.in_maintenance())
        self.c.create_task("恢复完成之后", prompt="p")

    def test_restore_failure_exits_maintenance(self):
        self.approved()
        p = self.bundle()

        def boom():
            raise RuntimeError("上层停服失败")

        with self.assertRaises(Exception):
            self.c.restore_from_bundle(p, before_replace=boom)
        self.assertFalse(self.db.in_maintenance(),
                         "恢复失败也必须退出维护模式，否则程序就永远连不上了")
        # 原库没有任何改动：身份仍启用
        row = self.db.one("SELECT enabled FROM executors WHERE id=?", (self.ex["id"],))
        self.assertEqual(row["enabled"], 1)


# ======================================================================
# R8 上层翻译：HTTP 503 / MCP 明确回错
# ======================================================================
class TestMaintenanceHttpSemantics(Base):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.c, watchdog_interval=None)
        self.cli = TestClient(self.app)

    def test_api_returns_503_during_maintenance(self):
        self.approved()
        hdr = {"Authorization": f"Bearer {self.ex['token']}"}
        with self.db.maintenance("恢复中"):
            r = self.cli.get("/api/v1/tasks/available", headers=hdr)
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.json().get("maintenance"))
        self.assertEqual(r.headers.get("retry-after"), "15")

    def test_mcp_returns_error_not_success_during_maintenance(self):
        tid = self.approved()  # noqa: F841 - claim 参数要用
        hdr = {"Authorization": f"Bearer {self.ex['token']}"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "council_claim_task",
                           "arguments": {"task_id": tid}}}
        with self.db.maintenance("恢复中"):
            r = self.cli.post("/mcp", json=body, headers=hdr)
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.json().get("maintenance"))
        # 关键：绝不能回一个「成功的 result」，否则执行器会以为票据到手了
        self.assertNotIn("result", r.json())

    def test_dispatch_tool_raises_maintenance_not_business_error(self):
        """即使身份已认证通过，进入维护后业务调用也必须抛 MaintenanceError。

        区分点很重要：业务失败（CouncilError）会让执行器认为「这次不可能成功」，
        维护模式必须表达成「这次没执行，稍后再试」。
        """
        from bigfish.server import dispatch_tool
        tid = self.approved()
        # 必须是**真正认证过**的身份（带 role_canonical）。rc8/R7-02 之后
        # 手工拼一个缺 role_canonical 的 dict 会被判成零权限角色，
        # 那样抛出来的就是「无权领取任务」，测不到维护语义。
        ex = self.c.authenticate(self.ex["token"], role="executor")
        with self.db.maintenance("恢复中"):
            with self.assertRaises(MaintenanceError):
                dispatch_tool(self.c, ex, "council_claim_task", {"task_id": tid})

    def test_health_reports_maintenance(self):
        with self.db.maintenance("恢复中"):
            r = self.cli.get("/health")
        self.assertEqual(r.json()["status"], "maintenance")
        self.assertTrue(r.json()["maintenance"])


if __name__ == "__main__":
    unittest.main()
