"""v0.1.1-rc4 新增回归：幂等契约 / MCP 协议语义 / 多轮执行票 / 审查绑定 / 端口预留.

这一批覆盖 rc4 的实际改动，每项都能对着一个具体缺陷复现：
  A. 幂等契约 6 项（含冒号的幂等键、同键同体、同键异体、重放不产生副作用、并发同键）
  B. MCP 协议语义 7 项（ping 带 id、ping 通知、缺 method、错 jsonrpc、参数类型、枚举、未知字段）
  C. 多轮执行票 6 项（旧票吊销、nonce 不重复、阻断后再领取换票、退回语义、verify 只收 SUBMITTED、拒绝 BLOCKED）
  D. 审查绑定 4 项（撤票不回落旧票、latest_approval 生效判定、先投票后送审、撤票退回议事）
  E. 执行器客户端 4 项（safe_submit 显式 task_id、BLOCKED 不算成功、SUBMITTED 算成功、同键重试）
  F. 辅助 6 项（list_tasks 状态集合/偏移/稳定排序、状态轻查询、预留端口与职责表不相交、角色白名单）

并发一律真线程 + Barrier；断言数据库最终状态，不断言「函数被调用过」。
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
from bigfish import roles as R
from bigfish.config import (DEFAULT_API_PORT, DEFAULT_PROXY_PORT, PORT_RESERVED,
                            PORT_ROLES, TEST_API_PORT)
from bigfish.core import Council, CouncilError
from bigfish.db import Database
from bigfish.executor_client import ExecutorClient
from bigfish.security import derive_ticket, load_or_create_key
from bigfish.server import create_app


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.key_path = self.root / "secrets" / "ticket.key"
        self.c = Council(self.db, key=load_or_create_key(self.key_path),
                         heartbeat_ttl=60, ticket_ttl=120,
                         ticket_max_ttl=3600, heartbeat_lease=180,
                         key_path=self.key_path)
        self.ex = self.c.register_executor("dsh-cloud")
        self.ex2 = self.c.register_executor("dsh-other")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def approved(self, title="演示任务", prompt="prompt v1") -> str:
        t = self.c.create_task(title, "验证", "独立目录", "low", prompt)
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid

    def status(self, tid) -> str:
        return self.c.get_task(tid)["status"]


# ======================================================================
# A. 幂等契约
# ======================================================================
class TestIdempotencyContract(Base):
    """E1：幂等键里可能含冒号，服务端不能按冒号全量切分。"""

    def test_idem_key_with_colon_replays_same_ticket(self):
        tid = self.approved()
        key = "claim:tenant:retry-1"          # 自带两个冒号的自定义前缀
        a = self.c.claim(tid, self.ex, idem_key=key)
        b = self.c.claim(tid, self.ex, idem_key=key)
        self.assertEqual(a["ticket"], b["ticket"], "含冒号的幂等键重放换票了")
        self.assertTrue(b.get("replayed"))

    def test_idem_key_with_colon_does_not_issue_second_ticket(self):
        tid = self.approved()
        self.c.claim(tid, self.ex, idem_key="a:b:c")
        self.c.claim(tid, self.ex, idem_key="a:b:c")
        n = self.db.one("SELECT COUNT(*) AS n FROM tickets WHERE task_id=?", (tid,))["n"]
        self.assertEqual(n, 1, "幂等重放不该签发第二张票")

    def test_same_key_same_body_returns_identical_response(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r1 = self.c.heartbeat(p["ticket"], self.ex, "running", "跑着呢", idem_key="hb-1")
        r2 = self.c.heartbeat(p["ticket"], self.ex, "running", "跑着呢", idem_key="hb-1")
        self.assertEqual(r1["server_time"], r2["server_time"], "重放返回了新时间戳")
        self.assertTrue(r2.get("replayed"))

    def test_same_key_different_body_rejected(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running", "A", idem_key="hb-x")
        with self.assertRaises(CouncilError) as cm:
            self.c.heartbeat(p["ticket"], self.ex, "running", "B", idem_key="hb-x")
        self.assertIn("不同请求", str(cm.exception))

    def test_replay_does_not_duplicate_events(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running", "一次", idem_key="hb-e")
        self.c.heartbeat(p["ticket"], self.ex, "running", "一次", idem_key="hb-e")
        n = self.db.one(
            "SELECT COUNT(*) AS n FROM events WHERE task_id=? AND kind=?",
            (tid, "run.heartbeat"))["n"]
        self.assertEqual(n, 1, "幂等重放重复写了事件")

    def test_concurrent_same_key_heartbeat_runs_once(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        barrier = threading.Barrier(4)
        results, errors = [], []

        def beat():
            try:
                barrier.wait(timeout=10)
                results.append(self.c.heartbeat(p["ticket"], self.ex, "running",
                                                "并发", idem_key="hb-race"))
            except Exception as exc:          # noqa: BLE001 - 记录后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=beat) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(20)
        self.assertEqual(errors, [], f"并发同键心跳抛错：{errors}")
        stamps = {r["server_time"] for r in results}
        self.assertEqual(len(stamps), 1, f"并发同键产生了不同响应：{stamps}")
        n = self.db.one(
            "SELECT COUNT(*) AS n FROM events WHERE task_id=? AND kind=?",
            (tid, "run.heartbeat"))["n"]
        self.assertEqual(n, 1, "并发同键心跳产生了重复事件")


# ======================================================================
# B. MCP 协议语义
# ======================================================================
class TestMcpProtocol(Base):
    """E2–E4：JSON-RPC 语义不能靠「看起来能用」蒙过去。"""

    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.c, watchdog_interval=None))
        self.hdr = {"Authorization": f"Bearer {self.ex['token']}"}

    def post(self, body, hdr=True):
        return self.client.post("/mcp", headers=self.hdr if hdr else {}, json=body)

    def test_ping_with_id_returns_result(self):
        r = self.post({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        self.assertEqual(r.status_code, 200, "ping 带 id 必须回 200 而不是 202")
        body = r.json()
        self.assertEqual(body.get("id"), 7)
        self.assertIn("result", body)

    def test_ping_as_notification_gets_202(self):
        r = self.post({"jsonrpc": "2.0", "method": "ping"})
        self.assertEqual(r.status_code, 202)

    def test_missing_method_is_invalid_request(self):
        r = self.post({"jsonrpc": "2.0", "id": 9})
        self.assertEqual(r.json()["error"]["code"], -32600, "缺 method 不是通知，是非法请求")

    def test_wrong_jsonrpc_version_rejected(self):
        r = self.post({"jsonrpc": "1.0", "id": 9, "method": "tools/list"})
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_tool_arg_type_error_is_isError(self):
        r = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "council_claim_task",
                                  "arguments": {"task_id": 123}}}).json()
        self.assertTrue(r["result"]["isError"], "参数类型错误没置 isError")
        self.assertIn("字符串", r["result"]["content"][0]["text"])

    def test_tool_enum_error_is_isError(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "council_heartbeat",
                                  "arguments": {"ticket": p["ticket"],
                                                "status": "nonsense"}}}).json()
        self.assertTrue(r["result"]["isError"])
        self.assertIn("running", r["result"]["content"][0]["text"])

    def test_unknown_argument_rejected(self):
        r = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "council_list_tasks",
                                  "arguments": {"bogus": 1}}}).json()
        self.assertTrue(r["result"]["isError"])
        self.assertIn("bogus", r["result"]["content"][0]["text"])


# ======================================================================
# C. 多轮执行票
# ======================================================================
class TestMultiRoundTickets(Base):
    """D：同一任务重新批准后必须换票，旧票不得复活。"""

    def test_reclaim_after_block_issues_new_ticket(self):
        tid = self.approved()
        first = self.c.claim(tid, self.ex)
        self.c.block(tid, "需要人工介入")
        self.assertEqual(self.status(tid), S.BLOCKED)
        # BLOCKED 是「副作用不明」，只能人工退回重做，不能直接再批准
        self.c.verify(tid, False, "确认无副作用，退回重做")
        self.assertEqual(self.status(tid), S.RETURNED)
        self.c.update_prompt(tid, "prompt v2", "第二轮")
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        second = self.c.claim(tid, self.ex)
        self.assertNotEqual(first["ticket"], second["ticket"],
                            "重新领取算出了同一张票，旧票可复活")
        with self.assertRaises(CouncilError):
            self.c.heartbeat(first["ticket"], self.ex, "running", "旧票")

    def test_old_ticket_revoked_on_new_claim(self):
        tid = self.approved()
        first = self.c.claim(tid, self.ex)
        self.c.heartbeat(first["ticket"], self.ex, "running", "跑着呢")
        # 派工中不允许直接改 Prompt：必须先阻断并人工退回（这条本身就是护栏）
        with self.assertRaises(CouncilError):
            self.c.update_prompt(tid, "prompt v2", "第二轮")
        self.c.block(tid, "需要改 Prompt")
        self.c.verify(tid, False, "退回重做")
        self.c.update_prompt(tid, "prompt v2", "第二轮")
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok2", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok2", "approve", kind="vote")
        self.c.approve(tid, "approve")
        second = self.c.claim(tid, self.ex)
        row = self.db.one("SELECT revoked_at FROM tickets WHERE id=?", (first["ticket_id"],))
        self.assertIsNotNone(row["revoked_at"], "旧票没被吊销")
        with self.assertRaises(CouncilError) as cm:
            self.c.heartbeat(first["ticket"], self.ex, "running", "旧票")
        self.assertIn("撤销", str(cm.exception))
        self.c.heartbeat(second["ticket"], self.ex, "running", "新票")

    def test_no_idem_key_uses_random_nonce(self):
        tid = self.approved()
        a = self.c.claim(tid, self.ex)
        self.c.block(tid, "阻断")
        self.c.verify(tid, False, "退回")
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        b = self.c.claim(tid, self.ex)
        self.assertNotEqual(a["ticket"], b["ticket"],
                            "无幂等键时应使用随机 nonce 派生票")

    def test_derive_ticket_is_deterministic_per_nonce(self):
        key = load_or_create_key(self.key_path)
        a = derive_ticket(key, "ex-1", "task-1", 1, "n1")
        b = derive_ticket(key, "ex-1", "task-1", 1, "n1")
        c = derive_ticket(key, "ex-1", "task-1", 1, "n2")
        self.assertEqual(a, b, "同一 nonce 派生结果不稳定")
        self.assertNotEqual(a, c, "不同 nonce 派生出同一张票")
        self.assertTrue(a.startswith("bfc_tk_"))

    def test_verify_accepts_only_submitted(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running")
        self.c.submit_result(p["ticket"], self.ex, "completed", log="done")
        self.assertEqual(self.status(tid), S.SUBMITTED)
        self.c.verify(tid, True, "验收通过")
        self.assertEqual(self.status(tid), S.DONE)

    def test_verify_cannot_accept_blocked(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.submit_result(p["ticket"], self.ex, "blocked", note="副作用不明")
        self.assertEqual(self.status(tid), S.BLOCKED)
        with self.assertRaises(CouncilError):
            self.c.verify(tid, True, "想直接标完成")
        self.assertEqual(self.status(tid), S.BLOCKED, "BLOCKED 被点成了 DONE")


# ======================================================================
# D. 审查绑定
# ======================================================================
class TestReviewBinding(Base):
    """C：撤票是立场撤退，不能回落到更早的赞成票。"""

    def test_revoke_latest_approve_does_not_fall_back(self):
        # 已批准状态下不再接受新票，所以必须在批准之前把「两张赞成票」造出来
        t = self.c.create_task("撤回退", "g", "s", "low", "p")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "first", "approve", kind="vote")
        self.c.add_review(tid, "gpt", "second", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        rows = [r for r in self.c.reviews(tid, self.c.get_task(tid)["current_version"])
                if r["model"] == "gpt" and r["verdict"] == "approve" and not r["revoked"]]
        self.c.revoke_review(rows[0]["id"])
        votes = self.c.vote_summary(tid, self.c.get_task(tid)["current_version"])
        self.assertIsNone(votes["gpt"]["verdict"], "撤票后回落到了更早的赞成票")
        self.assertTrue(votes["gpt"]["revoked"])
        self.assertFalse(votes["all_approved"])

    def test_latest_approval_not_effective_after_revoke(self):
        tid = self.approved()
        self.assertTrue(self.c.latest_approval(tid)["effective"])
        rows = [r for r in self.c.reviews(tid, self.c.get_task(tid)["current_version"])
                if r["model"] == "gpt" and r["verdict"] == "approve" and not r["revoked"]]
        self.c.revoke_review(rows[0]["id"])
        self.assertFalse(self.c.latest_approval(tid)["effective"],
                         "撤票退回议事后的历史批准仍显示生效")

    def test_votes_before_submit_go_straight_to_pending(self):
        t = self.c.create_task("先投票", "g", "s", "low", "p")
        tid = t["id"]
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.assertEqual(self.status(tid), S.DRAFT)
        self.c.submit_for_review(tid)
        self.assertEqual(self.status(tid), S.PENDING_APPROVAL,
                         "送审后没直接进待审批，卡在议事中")

    def test_revoke_after_dispatch_blocks_task(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertEqual(self.status(tid), S.RUNNING)
        rows = [r for r in self.c.reviews(tid, self.c.get_task(tid)["current_version"])
                if r["model"] == "gpt" and r["verdict"] == "approve" and not r["revoked"]]
        self.c.revoke_review(rows[0]["id"])
        self.assertIn(self.status(tid), (S.BLOCKED, S.IN_REVIEW),
                      "已派工任务撤票后没有安全阻断")
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"], self.ex, "running", "撤票后继续跑")


# ======================================================================
# E. 执行器客户端
# ======================================================================
class TestExecutorClient(Base):
    """E5：safe_submit 的「结果未知」处理必须是显式参数，不能靠 kwargs 碰运气。"""

    def client(self):
        return ExecutorClient(base_url="http://127.0.0.1:1", token="bfc_ex_test",
                              timeout=1)

    def test_safe_submit_requires_task_id(self):
        from bigfish.executor_client import _UncertainNetworkError
        cli = self.client()

        def boom(*a, **k):
            raise _UncertainNetworkError("模拟响应丢失")

        cli.submit = boom
        with self.assertRaises(RuntimeError) as cm:
            cli.safe_submit("bfc_tk_x", status="completed")
        self.assertIn("task_id", str(cm.exception),
                      "缺 task_id 时应明确说无法核对，而不是静默成功")

    def test_blocked_is_not_treated_as_success(self):
        cli = self.client()
        from bigfish.executor_client import _UncertainNetworkError

        def boom(*a, **k):
            raise _UncertainNetworkError("模拟响应丢失")

        cli.submit = boom
        cli.task_status = lambda tid: {"status": S.BLOCKED}
        with self.assertRaises(RuntimeError) as cm:
            cli.safe_submit("bfc_tk_x", task_id="task-1", status="completed")
        self.assertIn("不能证明", str(cm.exception),
                      "BLOCKED 被当成了提交成功（高危误判）")

    def test_submitted_is_treated_as_success(self):
        cli = self.client()
        from bigfish.executor_client import _UncertainNetworkError

        def boom(*a, **k):
            raise _UncertainNetworkError("模拟响应丢失")

        cli.submit = boom
        cli.task_status = lambda tid: {"status": S.SUBMITTED}
        out = cli.safe_submit("bfc_tk_x", task_id="task-1", status="completed")
        self.assertTrue(out["ok"])
        self.assertTrue(out["reconciled"])

    def test_safe_claim_reuses_same_key(self):
        cli = self.client()
        from bigfish.executor_client import _UncertainNetworkError
        seen = []

        def fake_claim(task_id, idem=None):
            seen.append(idem)
            if len(seen) == 1:
                raise _UncertainNetworkError("第一次丢了")
            return {"task_id": task_id, "ticket": "bfc_tk_z"}

        cli.claim = fake_claim
        out = cli.safe_claim("task-9", retries=1)
        self.assertEqual(out["ticket"], "bfc_tk_z")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1], "重试换了幂等键，会双发")


# ======================================================================
# F. 辅助：分页 / 轻查询 / 端口 / 角色
# ======================================================================
class TestAuxiliary(Base):
    def test_list_tasks_statuses_filter(self):
        a = self.c.create_task("甲", "g", "s", "low", "p")["id"]
        b = self.c.create_task("乙", "g", "s", "low", "p")["id"]
        self.c.submit_for_review(b)
        got = self.c.list_tasks(statuses={S.DRAFT})
        ids = [t["id"] for t in got]
        self.assertIn(a, ids)
        self.assertNotIn(b, ids)

    def test_list_tasks_offset_paging_is_stable(self):
        for i in range(5):
            self.c.create_task(f"任务{i}", "g", "s", "low", "p")
        page1 = self.c.list_tasks(limit=2, offset=0)
        page2 = self.c.list_tasks(limit=2, offset=2)
        seen = [t["id"] for t in page1] + [t["id"] for t in page2]
        self.assertEqual(len(seen), len(set(seen)), "翻页出现重复项")
        self.assertEqual(len(seen), 4)

    def test_list_tasks_order_is_deterministic(self):
        for i in range(4):
            self.c.create_task(f"同秒{i}", "g", "s", "low", "p")
        rows = self.c.list_tasks()
        a = [t["id"] for t in rows]
        b = [t["id"] for t in self.c.list_tasks()]
        self.assertEqual(a, b, "同一批数据两次查询顺序不同")
        # 排序契约：updated_at DESC，同时间戳用 id DESC 兜底
        expect = [t["id"] for t in sorted(rows, key=lambda r: (r["updated_at"], r["id"]),
                                          reverse=True)]
        self.assertEqual(a, expect, "排序不是 updated_at DESC, id DESC")

    def test_get_task_status_row_is_light(self):
        tid = self.c.create_task("轻查询", "g", "s", "low", "p")["id"]
        row = self.c.get_task_status_row(tid)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], S.DRAFT)
        self.assertEqual(row["status_label"], S.label(S.DRAFT))
        self.assertNotIn("reviews", row, "轻查询不该装配 reviews")
        self.assertIsNone(self.c.get_task_status_row("task-none"))

    def test_reserved_ports_do_not_collide(self):
        live = {p for p, _ in PORT_ROLES.values()}
        self.assertFalse(live & set(PORT_RESERVED),
                         f"预留端口与在用端口撞车：{live & set(PORT_RESERVED)}")
        self.assertEqual({DEFAULT_API_PORT, DEFAULT_PROXY_PORT, TEST_API_PORT}, live)

    def test_roles_db_whitelist(self):
        self.assertIn("executor", R.DB_ROLES)
        self.assertIn("admin", R.DB_ROLES)
        self.assertNotIn("mobile", R.DB_ROLES,
                         "未实现的 mobile 角色不该被默认当成执行器")


if __name__ == "__main__":
    unittest.main()