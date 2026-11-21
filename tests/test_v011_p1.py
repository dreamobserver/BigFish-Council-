"""v0.1.1 回归：P1 执行可靠性（Watchdog / 执行票续期 / 心跳幂等 / MCP 错误语义）."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from bigfish import state as S
from bigfish.core import Council, CouncilError
from bigfish.db import Database
from bigfish.security import load_or_create_key
from bigfish.server import create_app


class Base(unittest.TestCase):
    heartbeat_ttl = 60
    ticket_ttl = 120

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.c = Council(self.db, key=load_or_create_key(self.root / "secrets" / "ticket.key"),
                         heartbeat_ttl=self.heartbeat_ttl, ticket_ttl=self.ticket_ttl,
                         ticket_max_ttl=3600, heartbeat_lease=180)
        self.ex = self.c.register_executor("dsh-cloud")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def approved(self):
        t = self.c.create_task("演示任务", "验证", "独立目录", "low", "prompt v1")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid


class TestWatchdog(Base):
    """P1-1：看门狗独立于 GUI，后台自动阻断超时任务。"""

    def test_watchdog_blocks_without_gui(self):
        self.c.heartbeat_ttl = 1
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running")
        wd = self.c.start_watchdog(1.0)
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                if self.c.get_task(tid)["status"] == S.BLOCKED:
                    break
                time.sleep(0.3)
        finally:
            self.c.stop_watchdog()
        self.assertEqual(self.c.get_task(tid)["status"], S.BLOCKED,
                         "看门狗没有在后台把超时任务置为 BLOCKED")
        self.assertGreaterEqual(wd.sweeps, 0)

    def test_watchdog_singleton_per_db(self):
        a = self.c.start_watchdog(1.0)
        b = self.c.start_watchdog(1.0)
        self.assertIs(a, b, "同一数据库启动了多个看门狗实例")
        self.c.stop_watchdog()

    def test_watchdog_survives_sweep_error(self):
        """审计点名：原版只断言 stop 后 _watchdog 变空，异常吞没与线程存活都没验证。

        现在要求三件事同时成立：
        1) 连续多次扫描抛异常，线程仍然活着（不是第一次就死了）；
        2) errors 计数跟上，说明异常被数进去了而不是静默吞掉；
        3) 换回正常的 sweep 后还能真的继续工作（把超时任务打成 BLOCKED）。
        """
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("模拟扫描异常")

        self.c.sweep_timeouts = boom
        wd = self.c.start_watchdog(0.2)
        try:
            deadline = time.time() + 8
            while time.time() < deadline and wd.errors < 3:
                time.sleep(0.1)
            self.assertGreaterEqual(calls["n"], 3, "扫描根本没被反复调用")
            self.assertGreaterEqual(wd.errors, 3, "异常没有被计数")
            self.assertTrue(wd.is_alive(), "看门狗被单次异常打死了")
        finally:
            pass

        # 恢复正常扫描：必须还能把超时任务置为 BLOCKED
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running")
        del self.c.sweep_timeouts          # 还原成类方法
        self.c.heartbeat_ttl = 0           # 立刻算超时
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                if self.c.get_task(tid)["status"] == S.BLOCKED:
                    break
                time.sleep(0.1)
        finally:
            self.c.stop_watchdog()
        self.assertEqual(self.c.get_task(tid)["status"], S.BLOCKED,
                         "异常恢复后看门狗没有继续工作")
        self.assertFalse(self.c._watchdog)

    def test_health_reports_watchdog(self):
        self.c.start_watchdog(1.0)
        try:
            client = TestClient(create_app(self.c, watchdog_interval=None))
            with client:
                j = client.get("/health").json()
            self.assertIn("watchdog", j)
            self.assertTrue(j["watchdog"], "健康检查没反映看门狗状态")
        finally:
            self.c.stop_watchdog()


class TestTicketRenewal(Base):
    """P1-2：心跳必须续期，且受绝对上限约束。"""

    def test_heartbeat_extends_lease(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        first = p["expires_at"]
        time.sleep(0.05)
        hb = self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertGreater(hb["lease_expires_at"], first, "心跳没有续期")
        row = self.db.one("SELECT expires_at, renewed_at FROM tickets WHERE id=?",
                          (p["ticket_id"],))
        self.assertGreater(row["expires_at"], first)
        self.assertIsNotNone(row["renewed_at"])

    def test_lease_capped_by_max_ttl(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        hb = self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertLessEqual(hb["lease_expires_at"], hb["max_expires_at"],
                             "续租突破了绝对上限")

    def test_ticket_never_exceeds_max_ttl_from_issue(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.assertLessEqual(p["expires_at"], p["max_expires_at"])
        self.assertAlmostEqual(p["max_expires_at"] - p["expires_at"],
                               self.c.ticket_max_ttl - self.c.ticket_ttl, delta=2)

    def test_expired_lease_rejected(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.db.execute("UPDATE tickets SET expires_at=? WHERE id=?",
                        (time.time() - 1, p["ticket_id"]))
        with self.assertRaises(CouncilError) as ctx:
            self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertIn("过期", str(ctx.exception))

    def test_cancel_while_dispatched_needs_ack(self):
        """rc3 语义：已派工的任务取消后进入 CANCELLING，票故意保留——
        执行器必须还能心跳才知道要停；此时不允许照旧提交 completed。"""
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r = self.c.request_cancel(tid)
        self.assertEqual(self.c.get_task(tid)["status"], S.CANCELLING)
        self.assertFalse(r.get("final"))

        hb = self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertTrue(hb["cancel_ack_required"])
        self.assertTrue(hb["cancel"])

        with self.assertRaises(CouncilError) as ctx:
            self.c.submit_result(p["ticket"], self.ex, "completed")
        self.assertIn("取消", str(ctx.exception))

        out = self.c.confirm_cancel(p["ticket"], self.ex, "已停止")
        self.assertEqual(out["task_status"], S.CANCELLED)
        self.assertEqual(self.c.get_task(tid)["status"], S.CANCELLED)
        # 确认后票必须失效
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"], self.ex, "running")

    def test_update_prompt_forbidden_while_running(self):
        tid = self.approved()
        self.c.claim(tid, self.ex)
        with self.assertRaises(CouncilError):
            self.c.update_prompt(tid, "prompt v2", "中途改需求")

    def test_version_drift_invalidates_execution(self):
        """绕过 update_prompt 直接改版本（模拟外部进程/手工改库）时，
        心跳必须按版本比对拒绝，不能继续跑已作废的版本。"""
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.db.execute("UPDATE tasks SET current_version=current_version+1 WHERE id=?", (tid,))
        with self.assertRaises(CouncilError) as ctx:
            self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertIn("版本", str(ctx.exception))
        with self.assertRaises(CouncilError):
            self.c.submit_result(p["ticket"], self.ex, "completed", note="不该被接受")


class TestHeartbeatIdempotency(Base):
    """P1-3：同一幂等键的心跳必须真正幂等。"""

    def test_same_key_returns_identical_result(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r1 = self.c.heartbeat(p["ticket"], self.ex, "running", "第一次", idem_key="h1")
        r2 = self.c.heartbeat(p["ticket"], self.ex, "running", "第一次", idem_key="h1")
        self.assertEqual(r1["server_time"], r2["server_time"], "心跳幂等失效（时间不同）")
        self.assertTrue(r2.get("replayed"))
        n = self.db.one("SELECT COUNT(*) AS n FROM idempotency WHERE scope='heartbeat'")["n"]
        self.assertEqual(n, 1, "心跳幂等记录未落库")

    def test_same_key_logs_single_event(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        before = self.db.one(
            "SELECT COUNT(*) AS n FROM events WHERE kind='run.heartbeat'")["n"]
        self.c.heartbeat(p["ticket"], self.ex, "running", "ok", idem_key="h1")
        self.c.heartbeat(p["ticket"], self.ex, "running", "ok", idem_key="h1")
        after = self.db.one(
            "SELECT COUNT(*) AS n FROM events WHERE kind='run.heartbeat'")["n"]
        self.assertEqual(after - before, 1, "重放心跳重复写了事件")

    def test_different_key_is_not_replay(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running", "a", idem_key="h1")
        r2 = self.c.heartbeat(p["ticket"], self.ex, "running", "a", idem_key="h2")
        self.assertFalse(r2.get("replayed"))

    def test_key_reuse_with_different_body_rejected(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running", "a", idem_key="h1")
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"], self.ex, "paused", "b", idem_key="h1")

    def test_claim_replay_returns_same_ticket(self):
        tid = self.approved()
        a = self.c.claim(tid, self.ex, idem_key="c1")
        b = self.c.claim(tid, self.ex, idem_key="c1")
        self.assertEqual(a["ticket"], b["ticket"], "领取幂等重放换了票")
        self.assertTrue(b.get("replayed"))


class TestMcpSemantics(Base):
    """P1-4：MCP 业务失败必须 isError=true；鉴权失败必须是 JSON-RPC 错误。"""

    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.c, watchdog_interval=None))
        self.hdr = {"Authorization": f"Bearer {self.ex['token']}"}

    def _call(self, name, args):
        return self.client.post("/mcp", headers=self.hdr, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}).json()

    def test_business_error_sets_isError(self):
        t = self.c.create_task("未批准", prompt="p")
        r = self._call("council_claim_task", {"task_id": t["id"]})
        self.assertTrue(r["result"]["isError"], "业务失败没置 isError")
        self.assertIn("error", r["result"]["content"][0]["text"])

    def test_success_is_not_error(self):
        tid = self.approved()
        r = self._call("council_claim_task", {"task_id": tid})
        self.assertFalse(r["result"]["isError"])

    def test_missing_arg_is_error(self):
        r = self._call("council_claim_task", {})
        self.assertTrue(r["result"]["isError"])

    def test_bad_token_is_jsonrpc_error(self):
        r = self.client.post("/mcp", headers={"Authorization": "Bearer nope"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        self.assertNotIn("result", r)
        self.assertEqual(r["error"]["code"], -32001)

    def test_missing_token_is_jsonrpc_error(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                           "method": "tools/list"}).json()
        self.assertEqual(r["error"]["code"], -32001)

    def test_unknown_method(self):
        r = self.client.post("/mcp", headers=self.hdr,
                             json={"jsonrpc": "2.0", "id": 1, "method": "acme/nope"}).json()
        self.assertEqual(r["error"]["code"], -32601)

    def test_unknown_tool(self):
        r = self._call("council_approve_task", {"task_id": "x"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_no_approval_tool(self):
        r = self.client.post("/mcp", headers=self.hdr,
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        names = [t["name"] for t in r["result"]["tools"]]
        for n in names:
            self.assertNotIn("approve", n)
            self.assertNotIn("admin", n)
            self.assertNotIn("revoke", n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
