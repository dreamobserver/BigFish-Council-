"""v0.1.1 回归：P2 权限与审计（双模型身份绑定 / 角色分离 / 执行器边界 / 审计）."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from bigfish import roles as R
from bigfish import state as S
from bigfish.core import Council, CouncilError
from bigfish.db import Database
from bigfish.security import load_or_create_key
from bigfish.server import create_app


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.c = Council(self.db, key=load_or_create_key(self.root / "secrets" / "ticket.key"),
                         heartbeat_ttl=60, ticket_ttl=120)
        self.ex = self.c.register_executor("dsh-cloud")
        self.app = create_app(self.c, watchdog_interval=None)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def hdr(self, token):
        return {"Authorization": f"Bearer {token}"}


class TestRoleTable(unittest.TestCase):
    """角色权限表本身的行为。"""

    def test_canonical_roles(self):
        """rc7 起多了一个只读的 MOBILE_VIEWER（见 roles.py 注释）。

        这里不再硬编码 4，而是断言四个核心角色都在，且新增角色不会
        悄悄继承执行器权限。
        """
        for role in (R.ARCHITECT_GPT, R.REVIEWER_GEMINI, R.HUMAN_OWNER,
                     R.EXECUTOR_DSH):
            self.assertIn(role, R.ROLES)
        self.assertIn(R.MOBILE_VIEWER, R.ROLES)
        self.assertEqual(len(R.ROLES), len(set(R.ALL_ROLES)))

    def test_mobile_viewer_cannot_execute(self):
        """手机端只能看和留言，不能领任务/心跳/提交/审批/导出。

        这几条是 rc7 的硬约束：手机丢了也不能变成一台执行器。
        """
        for p in ("task.claim", "run.heartbeat", "run.submit", "approve",
                  "backup.export", "backup.restore", "executor.manage",
                  "ticket.issue", "task.cancel"):
            self.assertFalse(R.can(R.MOBILE_VIEWER, p), f"手机端不该有 {p}")
        for p in ("task.read", "event.read", "review.create"):
            self.assertTrue(R.can(R.MOBILE_VIEWER, p), f"手机端该有 {p}")

    def test_mobile_db_role_maps_to_viewer_not_executor(self):
        """未登记的 db_role 会被兜底成执行器；mobile 必须显式映射到只读。"""
        self.assertEqual(R.role_for_db_row("mobile"), R.MOBILE_VIEWER)
        self.assertNotEqual(R.role_for_db_row("mobile"), R.EXECUTOR_DSH)
        self.assertIn("mobile", R.DB_ROLES)

    def test_only_human_can_approve(self):
        self.assertTrue(R.can(R.HUMAN_OWNER, "approve"))
        for role in (R.ARCHITECT_GPT, R.REVIEWER_GEMINI, R.EXECUTOR_DSH):
            self.assertFalse(R.can(role, "approve"), f"{role} 不该有审批权")

    def test_executor_cannot_review_or_admin(self):
        for p in ("review.cast", "approve", "ticket.issue", "backup.restore",
                  "executor.manage", "audit.read"):
            self.assertFalse(R.can(R.EXECUTOR_DSH, p), f"执行器不该有 {p}")

    def test_reviewers_cannot_execute(self):
        for role in (R.ARCHITECT_GPT, R.REVIEWER_GEMINI):
            for p in ("task.claim", "run.heartbeat", "run.submit", "approve"):
                self.assertFalse(R.can(role, p), f"{role} 不该有 {p}")

    def test_require_raises(self):
        with self.assertRaises(PermissionError):
            R.require(R.EXECUTOR_DSH, "approve", "审批任务")

    def test_model_binding(self):
        self.assertEqual(R.model_for_role(R.ARCHITECT_GPT), "gpt")
        self.assertEqual(R.model_for_role(R.REVIEWER_GEMINI), "gemini")
        self.assertIsNone(R.model_for_role(R.HUMAN_OWNER))
        self.assertIsNone(R.model_for_role(R.EXECUTOR_DSH))


class TestReviewIdentity(Base):
    """P2-1：审查身份在服务端绑定，不能自报身份。"""

    def make(self):
        t = self.c.create_task("演示", prompt="p")
        self.c.submit_for_review(t["id"])
        return t["id"]

    def test_gpt_role_cannot_vote_as_gemini(self):
        tid = self.make()
        with self.assertRaises(CouncilError) as ctx:
            self.c.add_review(tid, "gemini", "假装", "approve", kind="vote",
                              actor=R.ARCHITECT_GPT)
        self.assertIn("只能以 gpt 身份投票", str(ctx.exception))

    def test_gemini_role_cannot_vote_as_gpt(self):
        tid = self.make()
        with self.assertRaises(CouncilError):
            self.c.add_review(tid, "gpt", "假装", "approve", kind="vote",
                              actor=R.REVIEWER_GEMINI)

    def test_bound_role_votes_recorded(self):
        tid = self.make()
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote", actor=R.ARCHITECT_GPT)
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote",
                          actor=R.REVIEWER_GEMINI)
        rows = {r["model"]: r for r in self.c.reviews(tid)}
        self.assertEqual(rows["gpt"]["actor"], R.ARCHITECT_GPT)
        self.assertEqual(rows["gemini"]["actor"], R.REVIEWER_GEMINI)
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)

    def test_human_paste_is_attributed_to_owner(self):
        tid = self.make()
        self.c.add_review(tid, "gpt", "人工粘贴", "approve", kind="vote")
        rows = self.c.reviews(tid)
        self.assertEqual(rows[0]["actor"], R.HUMAN_OWNER,
                         "人工转述必须记在 human_owner 名下")

    def test_executor_actor_rejected_at_approval(self):
        tid = self.make()
        # 绕过 add_review 的角色校验，直接把 actor 写成执行器（模拟历史脏数据）
        self.db.execute(
            "INSERT INTO reviews(task_id,version,model,kind,verdict,content,source,actor,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, 1, "gpt", "vote", "approve", "x", "manual", R.EXECUTOR_DSH, 0.0))
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote",
                          actor=R.REVIEWER_GEMINI)
        with self.assertRaises(CouncilError) as ctx:
            self.c.approve(tid, "approve")
        self.assertIn("执行器不能参与审查投票", str(ctx.exception))

    def test_non_human_cannot_approve(self):
        tid = self.make()
        for actor in (R.EXECUTOR_DSH, R.ARCHITECT_GPT, R.REVIEWER_GEMINI):
            with self.assertRaises(CouncilError):
                self.c.approve(tid, "approve", actor=actor)


class TestExecutorBoundary(Base):
    """P2-2：执行器令牌只能做执行器的事。"""

    def test_reviewer_token_rejected_by_executor_api(self):
        rv = self.c.register_executor("gpt-reviewer", role="reviewer")
        cli = TestClient(self.app)
        r = cli.get("/api/v1/tasks/available", headers=self.hdr(rv["token"]))
        self.assertEqual(r.status_code, 401)

    def test_executor_token_cannot_touch_admin_paths(self):
        cli = TestClient(self.app)
        h = self.hdr(self.ex["token"])
        for path in ("/api/v1/admin/approve", "/api/v1/executors", "/api/v1/audit"):
            r = cli.get(path, headers=h)
            self.assertIn(r.status_code, (404, 405), f"{path} 竟然存在")

    def test_mcp_executor_token_is_executor_role(self):
        d = self.c.authenticate(self.ex["token"], role="executor")
        self.assertEqual(d["role_canonical"], R.EXECUTOR_DSH)
        self.assertFalse(R.can(d["role_canonical"], "approve"))

    def test_admin_token_maps_to_human_owner(self):
        ad = self.c.register_executor("owner", role="admin")
        d = self.c.authenticate(ad["token"], role="admin")
        self.assertEqual(d["role_canonical"], R.HUMAN_OWNER)
        self.assertTrue(R.can(d["role_canonical"], "approve"))

    def test_revoked_executor_token_rejected(self):
        self.c.revoke_executor(self.ex["id"])
        with self.assertRaises(CouncilError):
            self.c.authenticate(self.ex["token"], role="executor")

    def test_token_hash_not_returned(self):
        d = self.c.authenticate(self.ex["token"], role="executor")
        self.assertNotIn("token_hash", d)

    def test_missing_permission_in_mcp_is_error(self):
        rv = self.c.register_executor("gemini-reviewer", role="reviewer")
        # reviewer 令牌过不了 /mcp 的 executor 鉴权
        cli = TestClient(self.app)
        r = cli.post("/mcp", headers=self.hdr(rv["token"]),
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        self.assertEqual(r["error"]["code"], -32001)


class TestAudit(Base):
    """审计必须覆盖敏感动作。"""

    def test_approval_and_registration_audited(self):
        t = self.c.create_task("演示", prompt="p")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        actions = [a["action"] for a in self.db.audits()]
        self.assertIn("approval.approve", actions)
        self.assertIn("executor.registered", actions)

    def test_revoke_executor_audited(self):
        self.c.revoke_executor(self.ex["id"])
        actions = [a["action"] for a in self.db.audits()]
        self.assertIn("executor.revoked", actions)

    def test_audit_has_actor_and_target(self):
        self.c.revoke_executor(self.ex["id"])
        row = [a for a in self.db.audits() if a["action"] == "executor.revoked"][0]
        self.assertEqual(row["actor"], R.HUMAN_OWNER)
        self.assertEqual(row["target"], self.ex["id"])

    def test_review_revoke_audited(self):
        t = self.c.create_task("演示", prompt="p")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        rid = self.c.reviews(tid)[0]["id"]
        self.c.revoke_review(rid)
        actions = [a["action"] for a in self.db.audits()]
        self.assertIn("review.revoked", actions)

    def test_audit_not_exposed_over_http(self):
        cli = TestClient(self.app)
        r = cli.get("/api/v1/audit", headers=self.hdr(self.ex["token"]))
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
