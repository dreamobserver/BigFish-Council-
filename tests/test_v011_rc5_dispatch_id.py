"""v0.1.1-rc5：服务端必须真的下发 dispatch_id，执行端收据才有稳定编号.

R5 的收据机制依赖「每次派工有一个稳定编号」。执行端已经有 dispatch_id_of()
的回退逻辑（没有就用 ticket_id），但那是降级路径；真正的编号必须由服务端给出，
否则一个客户端升级了、服务端没升级，两边对账的口径就对不上。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish import state as S
from bigfish.core import Council
from bigfish.db import Database
from bigfish.security import load_or_create_key


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

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def approved(self, title="演示"):
        tid = self.c.create_task(title, "验证", "目录", "low", "prompt")["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid


class TestDispatchId(Base):
    def test_claim_returns_dispatch_id(self):
        tid = self.approved()
        out = self.c.claim(tid, self.ex)
        self.assertIn("dispatch_id", out, "服务端没有下发 dispatch_id")
        self.assertEqual(out["dispatch_id"], out["ticket_id"])

    def test_replay_returns_same_dispatch_id(self):
        tid = self.approved()
        a = self.c.claim(tid, self.ex, idem_key="k1")
        b = self.c.claim(tid, self.ex, idem_key="k1")
        self.assertTrue(b.get("replayed"))
        self.assertEqual(a["dispatch_id"], b["dispatch_id"],
                         "幂等重放换了派工号，收据会被当成两次派工")

    def test_redispatch_gets_new_dispatch_id(self):
        """多轮执行：同一个任务重新批准后再领取，必须是新的派工号，
        否则执行端的旧收据会被新收据覆盖，看不出这是第二轮。"""
        tid = self.approved()
        first = self.c.claim(tid, self.ex)
        p = first
        self.c.heartbeat(p["ticket"], self.ex, "running")
        self.c.submit_result(p["ticket"], self.ex, "blocked", log="卡住")
        self.assertEqual(self.c.get_task(tid)["status"], S.BLOCKED)

        self.c.update_prompt(tid, "prompt v2", "改一版")
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        second = self.c.claim(tid, self.ex)
        self.assertNotEqual(first["dispatch_id"], second["dispatch_id"],
                            "重新派工复用了同一个派工号")


if __name__ == "__main__":
    unittest.main()
