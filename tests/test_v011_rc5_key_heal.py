"""v0.1.1-rc5 回归：R9（密钥自愈分支无单测 + 自愈后旧票仍能认证）.

审计指出的原文案是「恢复前的执行票全部失效」，而 ensure_key_consistency 里
根本没有碰 tickets 表——密钥换了，token_hash 还是旧值，旧票照样过认证。
这里把「自愈」当作一条完整的恢复路径来测：指纹对齐、票作废、幂等清空、审计留痕。
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish import state as S
from bigfish.backup import ensure_key_consistency
from bigfish.core import Council, CouncilError
from bigfish.db import Database
from bigfish.security import load_or_create_key


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.key_path = self.root / "secrets" / "ticket.key"
        self.key = load_or_create_key(self.key_path)
        self.c = Council(self.db, key=self.key, heartbeat_ttl=60, ticket_ttl=120,
                         ticket_max_ttl=3600, heartbeat_lease=180,
                         key_path=self.key_path)
        self.ex = self.c.register_executor("dsh-cloud")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def dispatched(self):
        tid = self.c.create_task("演示", "验证", "目录", "low", "prompt")["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid, self.c.claim(tid, self.ex, idem_key="k1")

    def fingerprint(self):
        row = self.db.one("SELECT value FROM meta WHERE key='key_sha256'")
        return row["value"] if row else None


class TestKeyFingerprintBootstrap(Base):
    def test_first_run_records_fingerprint_without_touching_tickets(self):
        """老库第一次跑到 rc5 时 meta 里没有指纹。补记不等于换钥，
        不能顺手把在途执行票全废掉。"""
        tid, p = self.dispatched()
        out = ensure_key_consistency(self.db, self.key_path)
        self.assertEqual(out["action"], "recorded")
        self.assertEqual(out["tickets_revoked"], 0)
        self.assertEqual(self.fingerprint(),
                         hashlib.sha256(self.key_path.read_bytes()).hexdigest())
        self.assertEqual(self.c.heartbeat(p["ticket"], self.ex, "running")["task_status"],
                         S.RUNNING)

    def test_second_run_is_noop(self):
        ensure_key_consistency(self.db, self.key_path)
        out = ensure_key_consistency(self.db, self.key_path)
        self.assertEqual(out["action"], "consistent")


class TestKeyRotationInvalidatesTickets(Base):
    def test_rotated_key_revokes_every_live_ticket(self):
        ensure_key_consistency(self.db, self.key_path)   # 先建立指纹
        tid, p = self.dispatched()
        self.key_path.write_bytes(b"\x00" * 32)          # 模拟磁盘密钥被动过
        out = ensure_key_consistency(self.db, self.key_path)
        self.assertEqual(out["action"], "rotated")
        self.assertGreaterEqual(out["tickets_revoked"], 1)
        rows = self.db.query("SELECT id, revoked_at FROM tickets WHERE task_id=?", (tid,))
        self.assertTrue(all(r["revoked_at"] for r in rows), "换钥后仍有存活的执行票")

    def test_old_ticket_cannot_authenticate_after_rotation(self):
        """核心断言：上一轮真的漏了这条。"""
        ensure_key_consistency(self.db, self.key_path)
        tid, p = self.dispatched()
        old = p["ticket"]
        self.assertEqual(self.c.heartbeat(old, self.ex, "running")["task_status"],
                         S.RUNNING)

        self.key_path.write_bytes(b"\xff" * 32)
        ensure_key_consistency(self.db, self.key_path)

        with self.assertRaises(CouncilError):
            self.c.heartbeat(old, self.ex, "running")
        status = self.c.get_task_status_row(tid)["status"]
        self.assertEqual(status, S.RUNNING,
                         "吊销票不该顺手改任务状态；状态由看门狗/人工决定")

    def test_rotation_purges_claim_idempotency(self):
        ensure_key_consistency(self.db, self.key_path)
        tid, p = self.dispatched()
        self.assertTrue(self.db.query("SELECT 1 FROM idempotency WHERE scope='claim'"))
        self.key_path.write_bytes(b"\x11" * 32)
        out = ensure_key_consistency(self.db, self.key_path)
        self.assertGreaterEqual(out["idempotency_purged"], 1)
        self.assertFalse(self.db.query("SELECT 1 FROM idempotency WHERE scope='claim'"))

    def test_rotation_leaves_audit_trail(self):
        ensure_key_consistency(self.db, self.key_path)
        self.dispatched()
        self.key_path.write_bytes(b"\x22" * 32)
        ensure_key_consistency(self.db, self.key_path)
        logs = [dict(r) for r in self.db.query("SELECT * FROM audit ORDER BY id")]
        healed = [r for r in logs if r["action"] == "db.key_healed"]
        self.assertTrue(healed, "自愈没有任何审计记录")
        self.assertIn("吊销", healed[-1]["detail"])

    def test_missing_key_file_regenerates_and_revokes(self):
        ensure_key_consistency(self.db, self.key_path)
        tid, p = self.dispatched()
        self.key_path.unlink()
        out = ensure_key_consistency(self.db, self.key_path)
        self.assertEqual(out["action"], "new-key")
        self.assertTrue(self.key_path.exists())
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"], self.ex, "running")


if __name__ == "__main__":
    unittest.main()
