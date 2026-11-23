"""只读低资源单元测试：状态机、审批闸门、执行票、幂等、超时阻断。

运行： python -m pytest -q   或   python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bigfish import state as S
from bigfish.core import Council, CouncilError
from bigfish.db import Database
from bigfish.security import load_or_create_key


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Database(Path(self.tmp.name) / "t.db")
        db.init_schema()
        key = load_or_create_key(Path(self.tmp.name) / "secrets" / "ticket.key")
        self.c = Council(db, key=key, heartbeat_ttl=2, ticket_ttl=60)
        self.ex = self.c.register_executor("dsh-cloud")

    def tearDown(self):
        self.c.db.close()
        self.tmp.cleanup()

    def make_reviewed(self):
        t = self.c.create_task("演示任务", "验证闭环", "只动独立目录", "low", "prompt v1")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "方案可行", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "无阻断问题", "approve", kind="vote")
        return tid


class TestReviewGate(Base):
    def test_needs_both_models(self):
        t = self.c.create_task("t", prompt="p")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        task = self.c.get_task(tid)
        self.assertEqual(task["status"], S.IN_REVIEW, "单模型通过不得进入 REVIEW_PASSED")

    def test_comment_is_not_vote(self):
        t = self.c.create_task("t", prompt="p")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "看着行", "approve", kind="comment")
        self.c.add_review(tid, "gemini", "看着行", "approve", kind="comment")
        self.assertEqual(self.c.get_task(tid)["status"], S.IN_REVIEW)

    def test_both_models_pass(self):
        tid = self.make_reviewed()
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)

    def test_reject_blocks(self):
        t = self.c.create_task("t", prompt="p")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "不行", "reject", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.assertNotEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)

    def test_vote_on_old_version_does_not_count(self):
        tid = self.make_reviewed()
        self.c.update_prompt(tid, "prompt v2", "改需求")
        task = self.c.get_task(tid)
        self.assertEqual(task["current_version"], 2)
        self.assertEqual(task["status"], S.IN_REVIEW)
        self.assertFalse(task["votes"]["all_approved"])


class TestApproval(Base):
    def test_cannot_approve_before_review(self):
        t = self.c.create_task("t", prompt="p")
        with self.assertRaises(CouncilError):
            self.c.approve(t["id"], "approve")

    def test_approve_then_claim(self):
        tid = self.make_reviewed()
        self.c.approve(tid, "approve", "人工批准")
        task = self.c.get_task(tid)
        self.assertEqual(task["status"], S.APPROVED)
        payload = self.c.claim(tid, self.ex)
        self.assertEqual(payload["prompt"], "prompt v1")
        self.assertTrue(payload["ticket"].startswith("bfc_tk_"))

    def test_claim_twice_fails(self):
        tid = self.make_reviewed()
        self.c.approve(tid, "approve")
        self.c.claim(tid, self.ex)
        with self.assertRaises(CouncilError):
            self.c.claim(tid, self.ex)

    def test_unapproved_task_not_listed(self):
        tid = self.make_reviewed()
        self.assertEqual(self.c.available_tasks(), [])
        self.c.approve(tid, "approve")
        self.assertEqual(len(self.c.available_tasks()), 1)


class TestExecution(Base):
    def test_full_loop_submitted_not_done(self):
        tid = self.make_reviewed()
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running", "ok")
        r = self.c.submit_result(p["ticket"], self.ex, "completed", "log...", "diff...", "tests pass")
        self.assertEqual(r["task_status"], S.SUBMITTED)
        self.assertEqual(self.c.get_task(tid)["status"], S.SUBMITTED, "回传后不得自动 DONE")
        self.c.verify(tid, True, "验收通过")
        self.assertEqual(self.c.get_task(tid)["status"], S.DONE)

    def test_bad_token_rejected(self):
        tid = self.make_reviewed()
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"][:-4] + "0000", self.ex)

    def test_revoked_ticket(self):
        tid = self.make_reviewed()
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        self.c.request_cancel(tid)
        with self.assertRaises(CouncilError):
            self.c.submit_result(p["ticket"], self.ex, "completed")

    def test_idempotent_submit(self):
        tid = self.make_reviewed()
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        r1 = self.c.submit_result(p["ticket"], self.ex, "completed", idem_key="k1")
        r2 = self.c.submit_result(p["ticket"], self.ex, "completed", idem_key="k1")
        self.assertEqual(r1, r2)

    def test_heartbeat_timeout_blocks(self):
        tid = self.make_reviewed()
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running")
        time.sleep(self.c.heartbeat_ttl + 1.2)
        blocked = self.c.sweep_timeouts()
        self.assertEqual(len(blocked), 1)
        task = self.c.get_task(tid)
        self.assertEqual(task["status"], S.BLOCKED)
        self.assertIn("心跳", task["blocked_reason"])

    def test_claim_after_board_offline_style(self):
        """任务板离线期间没有任何自动授权：APPROVED 才是唯一可领取状态。"""
        tid = self.make_reviewed()
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)
        with self.assertRaises(CouncilError):
            self.c.claim(tid, self.ex)


if __name__ == "__main__":
    unittest.main(verbosity=2)
