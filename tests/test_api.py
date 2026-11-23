"""API / MCP 端到端测试（TestClient，无需真实监听）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from bigfish import state as S
from bigfish.core import Council
from bigfish.db import Database
from bigfish.security import load_or_create_key
from bigfish.server import create_app


class TestApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Database(Path(self.tmp.name) / "t.db")
        db.init_schema()
        key = load_or_create_key(Path(self.tmp.name) / "secrets" / "ticket.key")
        self.c = Council(db, key=key, heartbeat_ttl=60, ticket_ttl=120)
        self.ex = self.c.register_executor("dsh-cloud")
        self.client = TestClient(create_app(self.c))
        self.hdr = {"Authorization": f"Bearer {self.ex['token']}"}

    def tearDown(self):
        self.c.db.close()
        self.tmp.cleanup()

    def _approved_task(self):
        t = self.c.create_task("模拟任务", "测闭环", "独立目录", "low", "最终 prompt")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve", "人工批准")
        return tid

    def test_health_open(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        # 健康检查不得泄露任务信息
        self.assertNotIn("tasks", r.json())

    def test_requires_token(self):
        self.assertEqual(self.client.get("/api/v1/tasks/available").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/tasks/available",
                                         headers={"Authorization": "Bearer bad"}).status_code, 401)

    def test_full_roundtrip(self):
        tid = self._approved_task()
        r = self.client.get("/api/v1/tasks/available", headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["tasks"]), 1)
        self.assertNotIn("prompt", r.json()["tasks"][0])

        r = self.client.post("/api/v1/tasks/claim", headers=self.hdr, json={"task_id": tid})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        ticket = body["ticket"]
        self.assertEqual(body["prompt"], "最终 prompt")

        r = self.client.post("/api/v1/runs/heartbeat", headers=self.hdr,
                             json={"ticket": ticket, "status": "running", "detail": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["cancel"])

        r = self.client.post("/api/v1/runs/result", headers=self.hdr,
                             json={"ticket": ticket, "status": "completed", "log": "done",
                                   "diff": "---", "tests": "pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["task_status"], S.SUBMITTED)

    def test_no_approval_tool_exposed(self):
        """远程 MCP 不得出现任何审批/提权工具。"""
        r = self.client.post("/mcp", headers=self.hdr,
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in r.json()["result"]["tools"]]
        self.assertEqual(sorted(names), sorted([
            "council_list_tasks", "council_claim_task", "council_heartbeat",
            "council_submit_result", "council_task_status"]))
        for n in names:
            self.assertNotIn("approve", n)
            self.assertNotIn("admin", n)

    def test_mcp_flow(self):
        tid = self._approved_task()
        r = self.client.post("/mcp", headers=self.hdr, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "council_claim_task", "arguments": {"task_id": tid}}})
        self.assertEqual(r.status_code, 200)
        text = r.json()["result"]["content"][0]["text"]
        self.assertIn("ticket", text)

    def test_mcp_rejects_unapproved(self):
        t = self.c.create_task("未批准", prompt="p")
        r = self.client.post("/mcp", headers=self.hdr, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "council_claim_task", "arguments": {"task_id": t["id"]}}})
        self.assertIn("error", r.json()["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
